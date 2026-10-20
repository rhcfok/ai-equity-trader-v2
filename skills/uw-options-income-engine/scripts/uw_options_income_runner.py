#!/usr/bin/env python3
"""
uw_options_income_runner.py
---------------------------
Stateless multi-strategy options income engine combining `option-davis` and
`put-option-strategy` with Unusual Whales fundamental, IV term-structure,
RSI(14), GEX wall, Dark Pool, and strike-level sweep filters.

Key design guarantees:
  1. 100% Stateless on Portfolio Holdings: Never reads `watchlist.position` or
     brokerage holdings. Every strategy (`WHEEL_CSP`, `WHEEL_CC`,
     `BULL_PUT_SPREAD`, `SHORT_STRANGLE`, `DIAGONAL_SPREAD`, `BEAR_CALL_SPREAD`)
     is evaluated purely on market structure, Unusual Whales data, J Law levels,
     GEX walls, and Dark Pool accumulation/distribution.
  2. Unusual Whales Replaces `equity_valuation_signal_daily`: Computes TTM EPS,
     trailing P/E, YoY EPS growth, PEG, earnings surprise %, and analyst target
     anchors directly from `/api/stock/{ticker}/earnings`, `/api/screener/analysts`,
     and optional `/api/companies/{ticker}/profile`.
  3. Strict NBBO Economics + Strike-Level Sweep Veto: Uses conservative bid/ask
     spread pricing and vetoes candidate short strikes hit by aggressive ask-side
     sweeps (`size > OI`) in `/api/option-trades`.
  4. Automated Lifecycle Tracking: Evaluates open `PROPOSED` / `ACTIVE` rows in
     `public."uw-option-opportunities"` for 50% max-profit auto-wins, 21-DTE
     Davis decision checkpoints, GEX wall breach defense alerts, and expiry settlement.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any, Callable, Iterable

import requests


UW_BASE_URL = "https://api.unusualwhales.com"
UW_CLIENT_API_ID = "100001"
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class UWValuationContext:
    symbol: str
    ttm_eps: float | None = None
    pe_ratio: float | None = None
    eps_growth_yoy_pct: float | None = None
    peg_ratio: float | None = None
    latest_surprise_pct: float | None = None
    analyst_target: float | None = None
    analyst_recent_action: str | None = None
    recent_downgrade_14d: bool = False
    valuation_tier: str = "not_meaningful"  # cheap|fair_low|fair_high|expensive|not_meaningful
    earnings_date: str | None = None
    earnings_report_time: str | None = None
    wheel_fundamental_ok: bool = False


@dataclass
class UWVolatilityContext:
    symbol: str
    iv7: float | None = None
    iv30: float | None = None
    iv60: float | None = None
    iv90: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    term_ratio_7_30: float | None = None
    term_ratio_30_60: float | None = None
    rsi_14: float | None = None
    stance_fit_score: float | None = None


@dataclass
class SymbolSignalBundle:
    symbol: str
    watchlist_category: str = "core"
    spot_price: float | None = None
    quote_as_of: str | None = None
    quote_status: str = "live"

    # Flow verdict + unusual option flow z-scores
    verdict: str = "no-data"
    verdict_reasons: list[str] = field(default_factory=list)
    key_flows: list[dict[str, Any]] = field(default_factory=list)
    flow_signal_date: str | None = None
    flow_bias: str | None = None
    bullish_z: float | None = None
    bearish_z: float | None = None
    net_bias_z: float | None = None

    # J Law v2 structure
    jlaw_run_date: str | None = None
    jlaw_classification: str | None = None
    jlaw_score: int | None = None
    jlaw_stop_loss: float | None = None
    jlaw_target_price: float | None = None
    option_score: int | None = None
    option_tier: str | None = None
    option_alert: bool | None = None

    # GEX snapshot
    gex_snapshot_ts: str | None = None
    gex_regime: str = "sub_resolution"
    gex_put_wall: float | None = None
    gex_call_wall: float | None = None
    gex_net_mm: float | None = None
    gex_gravity: str | None = None
    put_wall_pct: float | None = None
    call_wall_pct: float | None = None

    # Dark Pool daily
    dp_as_of: str | None = None
    dp_total_premium_mm: float | None = None
    dp_buy_mm: float | None = None
    dp_sell_mm: float | None = None
    dp_net_mm: float | None = None
    dp_blocks: int | None = None
    dp_top_level: float | None = None
    dp_top_level_mm: float | None = None
    dp_accumulating: bool | None = None

    # UW Fundamental & Volatility contexts
    valuation: UWValuationContext | None = None
    volatility: UWVolatilityContext | None = None


@dataclass
class OptionContractQuote:
    option_symbol: str
    underlying_symbol: str
    option_type: str  # "put" | "call"
    expiry: str       # YYYY-MM-DD
    dte: int
    strike: float
    bid: float
    ask: float
    delta: float | None
    iv: float | None
    open_interest: float
    volume: float = 0.0
    sweep_volume: float = 0.0
    ask_volume: float = 0.0
    bid_volume: float = 0.0


# ---------------------------------------------------------------------------
# Helper Utilities
# ---------------------------------------------------------------------------


def safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def safe_int(val: Any) -> int | None:
    f = safe_float(val)
    return int(round(f)) if f is not None else None


def parse_iso_date(val: Any) -> date | None:
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    s = str(val).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_occ_symbol(option_symbol: str) -> dict[str, Any] | None:
    if not option_symbol:
        return None
    m = OCC_RE.match(option_symbol.strip().upper())
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    yy = 2000 + int(yymmdd[:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    try:
        exp = date(yy, mm, dd).isoformat()
    except ValueError:
        return None
    return {
        "underlying_symbol": root,
        "expiry": exp,
        "option_type": "call" if cp == "C" else "put",
        "strike": int(strike_raw) / 1000.0,
    }


def is_us_option_symbol(symbol: str) -> bool:
    if not symbol:
        return False
    s = symbol.strip().upper()
    if "." in s or ":" in s or s.isdigit():
        return False
    return bool(re.match(r"^[A-Z]{1,6}$", s))


# ---------------------------------------------------------------------------
# Unusual Whales Fundamental & Volatility Evaluators
# ---------------------------------------------------------------------------


def compute_uw_valuation_context(
    symbol: str,
    spot_price: float | None,
    earnings_rows: list[dict[str, Any]],
    analyst_rows: list[dict[str, Any]],
    company_profile: dict[str, Any] | None = None,
    as_of_date: date | None = None,
) -> UWValuationContext:
    """
    Replaces `equity_valuation_signal_daily` using Unusual Whales endpoints:
      - /api/stock/{ticker}/earnings
      - /api/screener/analysts
      - /api/companies/{ticker}/profile (optional Advanced+ supplement)
    """
    ref_date = as_of_date or date.today()
    ctx = UWValuationContext(symbol=symbol)

    # 1. Quarterly EPS history & upcoming earnings date
    quarterly_actuals: list[tuple[date, float, float | None]] = []
    upcoming_dates: list[tuple[date, str | None]] = []

    for row in earnings_rows or []:
        rep_date = parse_iso_date(row.get("report_date") or row.get("ending_fiscal_quarter"))
        rep_time = row.get("report_time")
        if rep_date and rep_date >= ref_date:
            upcoming_dates.append((rep_date, str(rep_time) if rep_time else None))

        rtype = str(row.get("report_type") or "quarterly").lower()
        if rtype not in {"quarterly", "q", ""}:
            continue
        eps = safe_float(row.get("reported_eps") if "reported_eps" in row else row.get("actual_eps"))
        if eps is None:
            continue
        fdate = parse_iso_date(row.get("fiscal_date_ending") or row.get("ending_fiscal_quarter") or row.get("report_date"))
        if not fdate:
            continue
        surprise_pct = safe_float(row.get("surprise_percentage"))
        if surprise_pct is None:
            est = safe_float(row.get("estimated_eps") or row.get("street_mean_est"))
            if est is not None and abs(est) > 1e-6:
                surprise_pct = ((eps - est) / abs(est)) * 100.0
        quarterly_actuals.append((fdate, eps, surprise_pct))

    quarterly_actuals.sort(key=lambda x: x[0], reverse=True)
    if upcoming_dates:
        upcoming_dates.sort(key=lambda x: x[0])
        ctx.earnings_date = upcoming_dates[0][0].isoformat()
        ctx.earnings_report_time = upcoming_dates[0][1]

    if len(quarterly_actuals) >= 4:
        ctx.ttm_eps = round(sum(x[1] for x in quarterly_actuals[:4]), 4)
        ctx.latest_surprise_pct = (
            round(quarterly_actuals[0][2], 2) if quarterly_actuals[0][2] is not None else None
        )
        if len(quarterly_actuals) >= 8:
            prior_ttm = sum(x[1] for x in quarterly_actuals[4:8])
            if prior_ttm > 0:
                ctx.eps_growth_yoy_pct = round(((ctx.ttm_eps - prior_ttm) / abs(prior_ttm)) * 100.0, 2)

    # 2. Analyst targets & recent downgrade detection
    targets: list[float] = []
    recent_actions: list[tuple[date, str, str]] = []
    for row in analyst_rows or []:
        t = safe_float(row.get("target") or row.get("target_price"))
        if t is not None and t > 0:
            targets.append(t)
        act_date = parse_iso_date(row.get("timestamp") or row.get("date") or row.get("published_at"))
        action = str(row.get("action") or "").strip().lower()
        rec = str(row.get("recommendation") or "").strip().lower()
        if act_date:
            recent_actions.append((act_date, action, rec))

    if targets:
        ctx.analyst_target = round(float(statistics.median(targets)), 2)

    if recent_actions:
        recent_actions.sort(key=lambda x: x[0], reverse=True)
        ctx.analyst_recent_action = recent_actions[0][1] or recent_actions[0][2] or None
        for act_date, action, rec in recent_actions:
            if (ref_date - act_date).days <= 14:
                if action in {"downgraded", "target lowered"} or rec == "sell":
                    ctx.recent_downgrade_14d = True
                    break

    # 3. Supplement with optional /api/companies/{ticker}/profile if available
    if company_profile:
        if ctx.ttm_eps is None:
            ctx.ttm_eps = safe_float(company_profile.get("eps"))
        if ctx.pe_ratio is None:
            ctx.pe_ratio = safe_float(company_profile.get("pe_ratio"))
        if ctx.peg_ratio is None:
            ctx.peg_ratio = safe_float(company_profile.get("peg_ratio"))
        if ctx.analyst_target is None:
            ctx.analyst_target = safe_float(company_profile.get("analyst_target_price"))

    # 4. Derived P/E & PEG
    if ctx.pe_ratio is None and spot_price and ctx.ttm_eps and ctx.ttm_eps > 0:
        ctx.pe_ratio = round(spot_price / ctx.ttm_eps, 2)

    if (
        ctx.peg_ratio is None
        and ctx.pe_ratio is not None
        and ctx.pe_ratio > 0
        and ctx.eps_growth_yoy_pct is not None
        and ctx.eps_growth_yoy_pct > 0
    ):
        ctx.peg_ratio = round(ctx.pe_ratio / ctx.eps_growth_yoy_pct, 2)

    # 5. Deterministic valuation tier & Wheel fundamental gate
    if ctx.ttm_eps is None or ctx.ttm_eps <= 0:
        ctx.valuation_tier = "not_meaningful"
        ctx.wheel_fundamental_ok = False
        return ctx

    cheap_by_pe = ctx.pe_ratio is not None and ctx.pe_ratio <= 18.0
    cheap_by_peg = ctx.peg_ratio is not None and 0 < ctx.peg_ratio <= 1.2
    cheap_by_target = (
        spot_price is not None
        and ctx.analyst_target is not None
        and spot_price <= ctx.analyst_target * 0.85
    )

    fair_low_by_pe = ctx.pe_ratio is not None and ctx.pe_ratio <= 28.0
    fair_low_by_peg = ctx.peg_ratio is not None and 0 < ctx.peg_ratio <= 2.0
    fair_low_by_target = (
        spot_price is not None
        and ctx.analyst_target is not None
        and spot_price <= ctx.analyst_target * 0.95
    )

    fair_high_by_pe = ctx.pe_ratio is not None and ctx.pe_ratio <= 40.0
    fair_high_by_target = (
        spot_price is not None
        and ctx.analyst_target is not None
        and spot_price <= ctx.analyst_target * 1.05
    )

    if cheap_by_pe or cheap_by_peg or cheap_by_target:
        ctx.valuation_tier = "cheap"
    elif fair_low_by_pe or fair_low_by_peg or fair_low_by_target:
        ctx.valuation_tier = "fair_low"
    elif fair_high_by_pe or fair_high_by_target:
        ctx.valuation_tier = "fair_high"
    else:
        ctx.valuation_tier = "expensive"

    surprise_ok = ctx.latest_surprise_pct is None or ctx.latest_surprise_pct >= -2.0
    ctx.wheel_fundamental_ok = (
        ctx.ttm_eps > 0
        and not ctx.recent_downgrade_14d
        and surprise_ok
        and ctx.valuation_tier in {"cheap", "fair_low", "fair_high"}
    )
    return ctx


def compute_uw_volatility_context(
    symbol: str,
    interpolated_rows: list[dict[str, Any]],
    vol_context_payload: dict[str, Any] | None = None,
    rsi_rows: list[dict[str, Any]] | None = None,
    stance_payload: dict[str, Any] | None = None,
) -> UWVolatilityContext:
    """
    Builds UWVolatilityContext from:
      - /api/stock/{ticker}/interpolated-iv
      - /api/stock/{ticker}/volatility/context (optional)
      - /api/stock/{ticker}/technical-indicator/RSI
      - /api/stock/{ticker}/option-stance (optional)
    """
    vctx = UWVolatilityContext(symbol=symbol)

    by_days: dict[int, dict[str, Any]] = {}
    for row in interpolated_rows or []:
        d = safe_int(row.get("days"))
        if d is not None:
            by_days[d] = row

    def _pick_closest_days(target: int) -> dict[str, Any] | None:
        if not by_days:
            return None
        best_d = min(by_days.keys(), key=lambda k: abs(k - target))
        return by_days[best_d] if abs(best_d - target) <= 15 else None

    r7 = _pick_closest_days(7)
    r30 = _pick_closest_days(30)
    r60 = _pick_closest_days(60)
    r90 = _pick_closest_days(90)

    if r7:
        vctx.iv7 = safe_float(r7.get("volatility"))
    if r30:
        vctx.iv30 = safe_float(r30.get("volatility"))
        pct = safe_float(r30.get("percentile"))
        if pct is not None:
            vctx.iv_percentile = pct * 100.0 if pct <= 1.0 else pct
            vctx.iv_rank = vctx.iv_percentile
    if r60:
        vctx.iv60 = safe_float(r60.get("volatility"))
    if r90:
        vctx.iv90 = safe_float(r90.get("volatility"))

    if vol_context_payload:
        data = vol_context_payload.get("data") if isinstance(vol_context_payload.get("data"), dict) else vol_context_payload
        if isinstance(data, dict):
            if vctx.iv30 is None:
                vctx.iv30 = safe_float(data.get("iv30"))
            ivr = safe_float(data.get("iv_rank"))
            if ivr is not None:
                vctx.iv_rank = ivr * 100.0 if ivr <= 1.0 else ivr
            ivp = safe_float(data.get("iv_percentile"))
            if ivp is not None:
                vctx.iv_percentile = ivp * 100.0 if ivp <= 1.0 else ivp

    if vctx.iv7 and vctx.iv30 and vctx.iv30 > 0:
        vctx.term_ratio_7_30 = round(vctx.iv7 / vctx.iv30, 3)
    if vctx.iv30 and vctx.iv60 and vctx.iv60 > 0:
        vctx.term_ratio_30_60 = round(vctx.iv30 / vctx.iv60, 3)

    if rsi_rows:
        sorted_rsi = sorted(
            [r for r in rsi_rows if isinstance(r, dict)],
            key=lambda x: str(x.get("date") or ""),
            reverse=True,
        )
        for r in sorted_rsi:
            vals = r.get("values") if isinstance(r.get("values"), dict) else r
            rsi_val = safe_float(vals.get("RSI") or vals.get("rsi") or r.get("value"))
            if rsi_val is not None:
                vctx.rsi_14 = round(rsi_val, 2)
                break

    if stance_payload:
        contracts = stance_payload.get("contracts") or stance_payload.get("data") or []
        if isinstance(contracts, list) and contracts:
            scores = [safe_float(c.get("fit_score")) for c in contracts if isinstance(c, dict)]
            valid_scores = [s for s in scores if s is not None]
            if valid_scores:
                vctx.stance_fit_score = round(max(valid_scores), 2)

    return vctx


def evaluate_strike_flow_prints(
    option_trades: list[dict[str, Any]],
    option_type: str,
    target_strike: float,
    tolerance_pct: float = 0.03,
) -> tuple[int, int]:
    """
    Inspects `/api/option-trades` prints near `target_strike` (within ±tolerance_pct)
    and returns `(adverse_sweeps, confirming_whale_bids)`:
      - For a short put (`option_type == "put"`):
        * `adverse_sweeps`: aggressive ask-side put sweeps (`ask_vol > bid_vol` and
          (`size > open_interest` or `"RepeatedHits"` in report_flags or `premium >= 100000`))
        * `confirming_whale_bids`: institutional bid-side put writing (`bid_vol > ask_vol` and `premium >= 50000`)
      - For a short call (`option_type == "call"`):
        * `adverse_sweeps`: aggressive ask-side call sweeps blowing through the call ceiling
        * `confirming_whale_bids`: institutional bid-side call writing
    """
    adverse_sweeps = 0
    confirming_bids = 0
    if target_strike <= 0:
        return 0, 0

    want_type = option_type.lower()
    for t in option_trades or []:
        t_type = str(t.get("option_type") or "").lower()
        if t_type and not t_type.startswith(want_type[0]):
            continue
        strike = safe_float(t.get("strike"))
        if strike is None or abs(strike - target_strike) / target_strike > tolerance_pct:
            continue

        ask_vol = safe_float(t.get("ask_vol") or t.get("ask_volume")) or 0.0
        bid_vol = safe_float(t.get("bid_vol") or t.get("bid_volume")) or 0.0
        size = safe_float(t.get("size") or t.get("volume")) or 0.0
        oi = safe_float(t.get("open_interest")) or 0.0
        premium = safe_float(t.get("premium") or t.get("total_premium")) or 0.0
        flags = t.get("report_flags") or t.get("tags") or []
        if isinstance(flags, str):
            flags = [flags]
        flags_lower = {str(f).lower() for f in flags}

        is_aggressive_ask = ask_vol > bid_vol and (
            (oi > 0 and size > oi)
            or "repeatedhits" in flags_lower
            or "sweep" in flags_lower
            or premium >= 100_000
        )
        if is_aggressive_ask:
            adverse_sweeps += 1

        if bid_vol > ask_vol and premium >= 50_000:
            confirming_bids += 1

    return adverse_sweeps, confirming_bids


# ---------------------------------------------------------------------------
# Safety Floors, Ceilings & Earnings Clearance
# ---------------------------------------------------------------------------


def compute_safety_floor(bundle: SymbolSignalBundle) -> float | None:
    """
    Computes the structural put floor from valid support anchors below spot:
      `min(jlaw_stop_loss, gex_put_wall, dp_top_level when dp_accumulating)`
    """
    spot = bundle.spot_price
    if not spot or spot <= 0:
        return None
    anchors: list[float] = []
    if bundle.jlaw_stop_loss and 0 < bundle.jlaw_stop_loss < spot:
        anchors.append(bundle.jlaw_stop_loss)
    if (
        bundle.gex_regime != "sub_resolution"
        and bundle.gex_put_wall
        and 0 < bundle.gex_put_wall < spot
    ):
        anchors.append(bundle.gex_put_wall)
    if (
        bundle.dp_accumulating is True
        and bundle.dp_top_level
        and 0 < bundle.dp_top_level < spot
    ):
        anchors.append(bundle.dp_top_level)
    return round(min(anchors), 4) if anchors else None


def compute_safety_ceiling(bundle: SymbolSignalBundle) -> float | None:
    """
    Computes the structural call ceiling above spot:
      `max(gex_call_wall, jlaw_target_price)` when available, else `spot * 1.04`.
    """
    spot = bundle.spot_price
    if not spot or spot <= 0:
        return None
    anchors: list[float] = []
    if (
        bundle.gex_regime != "sub_resolution"
        and bundle.gex_call_wall
        and bundle.gex_call_wall > spot
    ):
        anchors.append(bundle.gex_call_wall)
    if bundle.jlaw_target_price and bundle.jlaw_target_price > spot:
        anchors.append(bundle.jlaw_target_price)
    return round(max(anchors), 4) if anchors else round(spot * 1.04, 4)


def is_earnings_clear(
    report_date: date,
    expiry_date: date,
    earnings_date_str: str | None,
    earnings_report_time: str | None = None,
) -> bool:
    """
    Returns True if there is no upcoming earnings report between `report_date`
    and `expiry_date`. If an earnings report falls ON `expiry_date`, only
    `postmarket` is treated as clear for that expiring contract.
    """
    edate = parse_iso_date(earnings_date_str)
    if not edate:
        return True
    if edate < report_date:
        return True
    if edate < expiry_date:
        return False
    if edate == expiry_date:
        return str(earnings_report_time or "").strip().lower() == "postmarket"
    return True


# ---------------------------------------------------------------------------
# Confidence Scoring (0 - 100)
# ---------------------------------------------------------------------------


def score_opportunity(
    strategy: str,
    bundle: SymbolSignalBundle,
    short_leg: OptionContractQuote,
    leg2: OptionContractQuote | None,
    return_on_risk: float | None,
    annualized_pct: float | None,
    safety_floor: float | None,
    safety_ceiling: float | None,
    adverse_sweeps: int,
    confirming_bids: int,
) -> tuple[float, str, dict[str, float]]:
    """
    Computes a deterministic 0-100 Confidence Score across 7 pillars:
      1. J Law Structure & Stop Buffer (0-20)
      2. UW Flow & Dark Pool Confirmation (0-20)
      3. GEX Map & Corridor Quality (0-15)
      4. UW Fundamental & Analyst Quality (0-15)
      5. Volatility & Term-Structure Fit (0-10)
      6. Execution & Liquidity Quality (0-10)
      7. Economics & Timing Quality (0-10)
    """
    spot = bundle.spot_price or short_leg.strike
    val = bundle.valuation or UWValuationContext(symbol=bundle.symbol)
    vol = bundle.volatility or UWVolatilityContext(symbol=bundle.symbol)

    # 1. J Law Structure & Stop Buffer (0-20)
    jlaw_pts = 8.0
    cls = str(bundle.jlaw_classification or "").strip().lower()
    if cls == "valid":
        jlaw_pts = 15.0
    elif cls == "watch":
        jlaw_pts = 11.0
    elif cls == "invalid":
        jlaw_pts = 3.0
    if bundle.jlaw_score is not None:
        jlaw_pts += min(3.0, max(0.0, (bundle.jlaw_score - 8) * 0.6))
    if safety_floor and short_leg.option_type == "put" and short_leg.strike <= safety_floor:
        jlaw_pts += 2.0
    elif safety_ceiling and short_leg.option_type == "call" and short_leg.strike >= safety_ceiling:
        jlaw_pts += 2.0
    jlaw_pts = round(min(20.0, jlaw_pts), 2)

    # 2. UW Flow & Dark Pool Confirmation (0-20)
    flow_pts = 8.0
    verdict = str(bundle.verdict or "no-data").lower()
    if strategy in {"WHEEL_CSP", "BULL_PUT_SPREAD", "DIAGONAL_SPREAD"}:
        if verdict == "bullish":
            flow_pts += 6.0
        elif verdict == "mixed":
            flow_pts += 3.0
    elif strategy in {"BEAR_CALL_SPREAD", "WHEEL_CC"}:
        if verdict == "bearish":
            flow_pts += 6.0
        elif verdict == "mixed":
            flow_pts += 4.0
    elif strategy == "SHORT_STRANGLE":
        if verdict in {"mixed", "no-data"}:
            flow_pts += 6.0

    if bundle.dp_accumulating is True and strategy in {"WHEEL_CSP", "BULL_PUT_SPREAD", "DIAGONAL_SPREAD"}:
        flow_pts += 4.0
    elif bundle.dp_accumulating is False and strategy in {"BEAR_CALL_SPREAD", "WHEEL_CC"}:
        flow_pts += 4.0

    if confirming_bids > 0:
        flow_pts += 2.0
    if adverse_sweeps > 0:
        flow_pts = max(0.0, flow_pts - 8.0)
    flow_pts = round(min(20.0, flow_pts), 2)

    # 3. GEX Map & Corridor Quality (0-15)
    gex_pts = 5.0
    if bundle.gex_regime == "positive":
        gex_pts = 11.0
    elif bundle.gex_regime == "negative":
        gex_pts = 8.0
    if bundle.gex_put_wall and short_leg.option_type == "put" and short_leg.strike <= bundle.gex_put_wall:
        gex_pts += 4.0
    if bundle.gex_call_wall and short_leg.option_type == "call" and short_leg.strike >= bundle.gex_call_wall:
        gex_pts += 4.0
    gex_pts = round(min(15.0, gex_pts), 2)

    # 4. UW Fundamental & Analyst Quality (0-15)
    fund_pts = 7.0
    if val.valuation_tier == "cheap":
        fund_pts = 13.0
    elif val.valuation_tier == "fair_low":
        fund_pts = 11.0
    elif val.valuation_tier == "fair_high":
        fund_pts = 9.0
    elif val.valuation_tier == "expensive":
        fund_pts = 6.0
    if val.latest_surprise_pct is not None and val.latest_surprise_pct > 2.0:
        fund_pts += 1.0
    if val.analyst_recent_action in {"upgraded", "target raised"}:
        fund_pts += 1.0
    if val.recent_downgrade_14d:
        fund_pts = max(0.0, fund_pts - 5.0)
    fund_pts = round(min(15.0, fund_pts), 2)

    # 5. Volatility & Term-Structure Fit (0-10)
    vol_pts = 5.0
    ivr = vol.iv_rank if vol.iv_rank is not None else 35.0
    if strategy == "DIAGONAL_SPREAD":
        if ivr < 35.0:
            vol_pts = 9.0
    elif strategy == "SHORT_STRANGLE":
        if ivr >= 50.0:
            vol_pts = 9.5
    else:
        if ivr >= 40.0:
            vol_pts = 9.0
        elif ivr >= 25.0:
            vol_pts = 7.5
    if vol.stance_fit_score is not None and vol.stance_fit_score >= 3.5:
        vol_pts += 1.0
    vol_pts = round(min(10.0, vol_pts), 2)

    # 6. Execution & Liquidity Quality (0-10)
    mid = max(0.05, (short_leg.bid + short_leg.ask) / 2.0)
    spread_pct = (short_leg.ask - short_leg.bid) / mid
    liq_pts = 5.0
    if short_leg.open_interest >= 1000:
        liq_pts += 3.0
    elif short_leg.open_interest >= 250:
        liq_pts += 1.5
    if spread_pct <= 0.08:
        liq_pts += 2.0
    elif spread_pct <= 0.15:
        liq_pts += 1.0
    liq_pts = round(min(10.0, liq_pts), 2)

    # 7. Economics & Timing Quality (0-10)
    econ_pts = 5.0
    if return_on_risk is not None and return_on_risk >= 0.22:
        econ_pts += 3.0
    elif annualized_pct is not None and annualized_pct >= 24.0:
        econ_pts += 3.0
    if vol.rsi_14 is not None:
        if strategy in {"WHEEL_CSP", "BULL_PUT_SPREAD"} and 35.0 <= vol.rsi_14 <= 55.0:
            econ_pts += 2.0
        elif strategy == "WHEEL_CC" and vol.rsi_14 >= 60.0:
            econ_pts += 2.0
        elif strategy == "SHORT_STRANGLE" and 42.0 <= vol.rsi_14 <= 58.0:
            econ_pts += 2.0
    econ_pts = round(min(10.0, econ_pts), 2)

    total = round(jlaw_pts + flow_pts + gex_pts + fund_pts + vol_pts + liq_pts + econ_pts, 2)
    if total >= 85.0:
        band = "STRONG"
    elif total >= 70.0:
        band = "HIGHER"
    elif total >= 60.0:
        band = "MODERATE"
    else:
        band = "LOWER"

    components = {
        "jlaw_structure": jlaw_pts,
        "flow_darkpool": flow_pts,
        "gex_corridor": gex_pts,
        "uw_fundamental": fund_pts,
        "volatility_fit": vol_pts,
        "liquidity": liq_pts,
        "economics_timing": econ_pts,
    }
    return total, band, components


# ---------------------------------------------------------------------------
# Stateless Strategy Candidate Builders
# ---------------------------------------------------------------------------


def _make_opportunity_row(
    report_date: date,
    run_id: str,
    strategy: str,
    dte_bucket: str,
    bundle: SymbolSignalBundle,
    short_leg: OptionContractQuote,
    leg2: OptionContractQuote | None,
    leg2_role: str | None,
    spread_width: float | None,
    net_entry_price: float,
    entry_credit: float | None,
    entry_debit: float | None,
    collateral: float,
    max_loss: float,
    return_on_risk: float | None,
    annualized_pct: float | None,
    break_even_low: float | None,
    break_even_high: float | None,
    safety_floor: float | None,
    safety_ceiling: float | None,
    adverse_sweeps: int,
    confirming_bids: int,
    notes: str,
) -> dict[str, Any]:
    val = bundle.valuation or UWValuationContext(symbol=bundle.symbol)
    vol = bundle.volatility or UWVolatilityContext(symbol=bundle.symbol)
    second_strike_str = f"{leg2.strike:.2f}" if leg2 else "NA"
    opp_key = (
        f"{report_date.isoformat()}|{strategy}|{dte_bucket}|{bundle.symbol}|"
        f"{short_leg.expiry}|{short_leg.strike:.2f}|{second_strike_str}"
    )

    conf_score, score_band, score_components = score_opportunity(
        strategy=strategy,
        bundle=bundle,
        short_leg=short_leg,
        leg2=leg2,
        return_on_risk=return_on_risk,
        annualized_pct=annualized_pct,
        safety_floor=safety_floor,
        safety_ceiling=safety_ceiling,
        adverse_sweeps=adverse_sweeps,
        confirming_bids=confirming_bids,
    )

    cautions: list[str] = []
    if bundle.gex_regime == "sub_resolution":
        cautions.append("GEX_SUB_RESOLUTION")
    if val.valuation_tier == "expensive":
        cautions.append("VALUATION_EXPENSIVE")
    if vol.term_ratio_7_30 and vol.term_ratio_7_30 > 1.15:
        cautions.append("FRONT_IV_ELEVATED")

    return {
        "opportunity_key": opp_key,
        "run_id": run_id,
        "report_date": report_date.isoformat(),
        "status": "PROPOSED",
        "outcome": None,
        "win": None,
        "strategy": strategy,
        "dte_bucket": dte_bucket,
        "symbol": bundle.symbol,
        "watchlist_category": bundle.watchlist_category,
        "spot_price": bundle.spot_price,
        "quote_as_of": bundle.quote_as_of,
        "quote_status": bundle.quote_status,
        "short_option_symbol": short_leg.option_symbol,
        "short_type": short_leg.option_type,
        "short_expiry": short_leg.expiry,
        "short_dte": short_leg.dte,
        "short_strike": short_leg.strike,
        "short_delta": short_leg.delta,
        "short_iv": short_leg.iv,
        "short_oi": short_leg.open_interest,
        "short_bid": short_leg.bid,
        "short_ask": short_leg.ask,
        "leg2_role": leg2_role,
        "leg2_option_symbol": leg2.option_symbol if leg2 else None,
        "leg2_type": leg2.option_type if leg2 else None,
        "leg2_expiry": leg2.expiry if leg2 else None,
        "leg2_dte": leg2.dte if leg2 else None,
        "leg2_strike": leg2.strike if leg2 else None,
        "leg2_delta": leg2.delta if leg2 else None,
        "leg2_iv": leg2.iv if leg2 else None,
        "leg2_oi": leg2.open_interest if leg2 else None,
        "leg2_bid": leg2.bid if leg2 else None,
        "leg2_ask": leg2.ask if leg2 else None,
        "spread_width": spread_width,
        "net_entry_price": round(net_entry_price, 4),
        "entry_credit": round(entry_credit, 4) if entry_credit is not None else None,
        "entry_debit": round(entry_debit, 4) if entry_debit is not None else None,
        "collateral": round(collateral, 4),
        "max_loss": round(max_loss, 4),
        "return_on_risk": round(return_on_risk, 4) if return_on_risk is not None else None,
        "annualized_pct": round(annualized_pct, 2) if annualized_pct is not None else None,
        "break_even_low": round(break_even_low, 4) if break_even_low is not None else None,
        "break_even_high": round(break_even_high, 4) if break_even_high is not None else None,
        "davis_annualized_ok": bool(annualized_pct is not None and annualized_pct >= 20.0),
        "delta_band_ok": True,
        "safety_floor_price": safety_floor,
        "safety_ceiling_price": safety_ceiling,
        "uw_ttm_eps": val.ttm_eps,
        "uw_pe_ratio": val.pe_ratio,
        "uw_eps_growth_yoy_pct": val.eps_growth_yoy_pct,
        "uw_peg_ratio": val.peg_ratio,
        "uw_latest_surprise_pct": val.latest_surprise_pct,
        "uw_analyst_target": val.analyst_target,
        "uw_analyst_recent_action": val.analyst_recent_action,
        "uw_valuation_tier": val.valuation_tier,
        "earnings_date": val.earnings_date,
        "earnings_report_time": val.earnings_report_time,
        "earnings_clear": True,
        "uw_iv7": vol.iv7,
        "uw_iv30": vol.iv30,
        "uw_iv60": vol.iv60,
        "uw_iv90": vol.iv90,
        "uw_iv_rank": vol.iv_rank,
        "uw_iv_percentile": vol.iv_percentile,
        "uw_term_ratio_7_30": vol.term_ratio_7_30,
        "uw_term_ratio_30_60": vol.term_ratio_30_60,
        "uw_rsi_14": vol.rsi_14,
        "uw_stance_fit_score": vol.stance_fit_score,
        "strike_adverse_sweeps": adverse_sweeps,
        "strike_whale_put_bids": confirming_bids,
        "verdict": bundle.verdict,
        "verdict_reasons": bundle.verdict_reasons,
        "key_flows": bundle.key_flows,
        "flow_signal_date": bundle.flow_signal_date,
        "flow_bias": bundle.flow_bias,
        "bullish_z": bundle.bullish_z,
        "bearish_z": bundle.bearish_z,
        "net_bias_z": bundle.net_bias_z,
        "jlaw_run_date": bundle.jlaw_run_date,
        "jlaw_classification": bundle.jlaw_classification,
        "jlaw_score": bundle.jlaw_score,
        "jlaw_stop_loss": bundle.jlaw_stop_loss,
        "jlaw_target_price": bundle.jlaw_target_price,
        "option_score": bundle.option_score,
        "option_tier": bundle.option_tier,
        "option_alert": bundle.option_alert,
        "gex_snapshot_ts": bundle.gex_snapshot_ts,
        "gex_regime": bundle.gex_regime,
        "gex_put_wall": bundle.gex_put_wall,
        "gex_call_wall": bundle.gex_call_wall,
        "gex_net_mm": bundle.gex_net_mm,
        "gex_gravity": bundle.gex_gravity,
        "put_wall_pct": bundle.put_wall_pct,
        "call_wall_pct": bundle.call_wall_pct,
        "dp_as_of": bundle.dp_as_of,
        "dp_total_premium_mm": bundle.dp_total_premium_mm,
        "dp_buy_mm": bundle.dp_buy_mm,
        "dp_sell_mm": bundle.dp_sell_mm,
        "dp_net_mm": bundle.dp_net_mm,
        "dp_blocks": bundle.dp_blocks,
        "dp_top_level": bundle.dp_top_level,
        "dp_top_level_mm": bundle.dp_top_level_mm,
        "dp_accumulating": bundle.dp_accumulating,
        "confidence_score": conf_score,
        "score_band": score_band,
        "score_components": score_components,
        "caution": "|".join(cautions) if cautions else None,
        "notes": notes,
    }


def generate_opportunities_for_symbol(
    report_date: date,
    run_id: str,
    bundle: SymbolSignalBundle,
    contracts: list[OptionContractQuote],
    option_trades: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluates a symbol's stateless signal bundle and option chain across all
    5 core strategy families:
      1. WHEEL_CSP (CORE ~32 DTE & TACTICAL 5-10 DTE)
      2. WHEEL_CC  (Stateless Covered Call ceiling reference board)
      3. BULL_PUT_SPREAD (5-wide if spot < $150, 10-wide if spot >= $150, |Δ| <= 0.28)
      4. SHORT_STRANGLE (30-45 DTE in positive GEX + IV Rank >= 45%)
      5. DIAGONAL_SPREAD (60-95 DTE 0.70-0.82Δ long call + 7-21 DTE 0.20-0.30Δ short call in low IV)
      6. BEAR_CALL_SPREAD (Bearish flow + DP distribution call credit spread)
    """
    if not is_us_option_symbol(bundle.symbol):
        return []
    spot = bundle.spot_price
    if not spot or spot <= 0 or not contracts:
        return []

    val = bundle.valuation or UWValuationContext(symbol=bundle.symbol)
    vol = bundle.volatility or UWVolatilityContext(symbol=bundle.symbol)
    trades = option_trades or []

    safety_floor = compute_safety_floor(bundle)
    safety_ceiling = compute_safety_ceiling(bundle)
    verdict = str(bundle.verdict or "no-data").strip().lower()
    jlaw_cls = str(bundle.jlaw_classification or "").strip().lower()
    ivr = vol.iv_rank if vol.iv_rank is not None else 35.0
    rsi = vol.rsi_14 if vol.rsi_14 is not None else 50.0
    term_7_30 = vol.term_ratio_7_30 if vol.term_ratio_7_30 is not None else 1.0
    term_30_60 = vol.term_ratio_30_60 if vol.term_ratio_30_60 is not None else 1.0

    results: list[dict[str, Any]] = []

    puts = [
        c for c in contracts
        if c.option_type == "put" and c.bid > 0 and c.ask >= c.bid and c.dte >= 3
    ]
    calls = [
        c for c in contracts
        if c.option_type == "call" and c.bid > 0 and c.ask >= c.bid and c.dte >= 5
    ]

    # ------------------------------------------------------------------
    # 1. WHEEL_CSP (CORE 25-39 DTE & TACTICAL 3-10 DTE)
    # ------------------------------------------------------------------
    wheel_csp_regime_ok = (
        bundle.watchlist_category in {"core", "satellite", "watch1"}
        and jlaw_cls in {"valid", "watch"}
        and verdict in {"bullish", "mixed"}
        and val.wheel_fundamental_ok
        and ivr >= 25.0
        and term_7_30 <= 1.25
        and 32.0 <= rsi <= 60.0
        and bundle.dp_accumulating is not False
        and safety_floor is not None
    )

    if wheel_csp_regime_ok and safety_floor is not None:
        # 1A: CORE (~32 DTE, 0.18 <= |delta| <= 0.28, annualized >= 20%)
        core_puts = [
            p for p in puts
            if 25 <= p.dte <= 39
            and p.delta is not None
            and 0.18 <= abs(p.delta) <= 0.28
            and p.strike <= safety_floor
            and is_earnings_clear(report_date, date.fromisoformat(p.expiry), val.earnings_date, val.earnings_report_time)
        ]
        best_core_row: dict[str, Any] | None = None
        for p in core_puts:
            ann_pct = (p.bid / p.strike) * (365.0 / p.dte) * 100.0
            if ann_pct < 20.0:
                continue
            adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "put", p.strike)
            if adv_sweeps > 0:
                continue
            ror = p.bid / max(0.01, p.strike - p.bid)
            row = _make_opportunity_row(
                report_date=report_date,
                run_id=run_id,
                strategy="WHEEL_CSP",
                dte_bucket="CORE",
                bundle=bundle,
                short_leg=p,
                leg2=None,
                leg2_role=None,
                spread_width=None,
                net_entry_price=p.bid,
                entry_credit=p.bid,
                entry_debit=None,
                collateral=p.strike,
                max_loss=p.strike - p.bid,
                return_on_risk=ror,
                annualized_pct=ann_pct,
                break_even_low=p.strike - p.bid,
                break_even_high=None,
                safety_floor=safety_floor,
                safety_ceiling=safety_ceiling,
                adverse_sweeps=adv_sweeps,
                confirming_bids=whale_bids,
                notes=f"Core ~32d Wheel CSP anchored <= safety floor {safety_floor:.2f} ({val.valuation_tier})",
            )
            if not best_core_row or row["confidence_score"] > best_core_row["confidence_score"]:
                best_core_row = row
        if best_core_row:
            results.append(best_core_row)

        # 1B: TACTICAL (3-10 DTE, |delta| < 0.20, annualized >= 18%)
        if term_7_30 <= 1.18:
            tac_puts = [
                p for p in puts
                if 3 <= p.dte <= 10
                and p.delta is not None
                and 0.06 <= abs(p.delta) < 0.20
                and p.strike <= safety_floor
                and is_earnings_clear(report_date, date.fromisoformat(p.expiry), val.earnings_date, val.earnings_report_time)
            ]
            best_tac_row: dict[str, Any] | None = None
            for p in tac_puts:
                ann_pct = (p.bid / p.strike) * (365.0 / p.dte) * 100.0
                if ann_pct < 18.0:
                    continue
                adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "put", p.strike)
                if adv_sweeps > 0:
                    continue
                ror = p.bid / max(0.01, p.strike - p.bid)
                row = _make_opportunity_row(
                    report_date=report_date,
                    run_id=run_id,
                    strategy="WHEEL_CSP",
                    dte_bucket="TACTICAL",
                    bundle=bundle,
                    short_leg=p,
                    leg2=None,
                    leg2_role=None,
                    spread_width=None,
                    net_entry_price=p.bid,
                    entry_credit=p.bid,
                    entry_debit=None,
                    collateral=p.strike,
                    max_loss=p.strike - p.bid,
                    return_on_risk=ror,
                    annualized_pct=ann_pct,
                    break_even_low=p.strike - p.bid,
                    break_even_high=None,
                    safety_floor=safety_floor,
                    safety_ceiling=safety_ceiling,
                    adverse_sweeps=adv_sweeps,
                    confirming_bids=whale_bids,
                    notes=f"Tactical 3-10d Wheel CSP <20d delta below safety floor {safety_floor:.2f}",
                )
                if not best_tac_row or row["confidence_score"] > best_tac_row["confidence_score"]:
                    best_tac_row = row
            if best_tac_row:
                results.append(best_tac_row)

    # ------------------------------------------------------------------
    # 2. WHEEL_CC (Stateless Covered Call Reference Board at/above GEX Call Wall)
    # ------------------------------------------------------------------
    reasons_joined = " ".join(bundle.verdict_reasons or []).lower()
    wheel_cc_regime_ok = (
        bundle.watchlist_category in {"core", "satellite"}
        and ivr >= 20.0
        and safety_ceiling is not None
        and (
            rsi >= 60.0
            or "covered-call" in reasons_joined
            or verdict in {"mixed", "bearish"}
        )
    )
    if wheel_cc_regime_ok and safety_ceiling is not None:
        cc_calls = [
            c for c in calls
            if 14 <= c.dte <= 35
            and c.delta is not None
            and 0.16 <= abs(c.delta) <= 0.28
            and c.strike >= safety_ceiling
            and is_earnings_clear(report_date, date.fromisoformat(c.expiry), val.earnings_date, val.earnings_report_time)
        ]
        best_cc_row: dict[str, Any] | None = None
        for c in cc_calls:
            ann_pct = (c.bid / spot) * (365.0 / c.dte) * 100.0
            if ann_pct < 12.0:
                continue
            adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "call", c.strike)
            if adv_sweeps > 0:
                continue
            ror = c.bid / spot
            row = _make_opportunity_row(
                report_date=report_date,
                run_id=run_id,
                strategy="WHEEL_CC",
                dte_bucket="CORE",
                bundle=bundle,
                short_leg=c,
                leg2=None,
                leg2_role=None,
                spread_width=None,
                net_entry_price=c.bid,
                entry_credit=c.bid,
                entry_debit=None,
                collateral=spot,
                max_loss=max(0.01, spot - c.bid),
                return_on_risk=ror,
                annualized_pct=ann_pct,
                break_even_low=spot - c.bid,
                break_even_high=c.strike + c.bid,
                safety_floor=safety_floor,
                safety_ceiling=safety_ceiling,
                adverse_sweeps=adv_sweeps,
                confirming_bids=whale_bids,
                notes=f"Stateless Wheel Call reference >= ceiling {safety_ceiling:.2f} (RSI {rsi:.1f})",
            )
            if not best_cc_row or row["confidence_score"] > best_cc_row["confidence_score"]:
                best_cc_row = row
        if best_cc_row:
            results.append(best_cc_row)

    # ------------------------------------------------------------------
    # 3. BULL_PUT_SPREAD (Adaptive 5-wide < $150 / 10-wide >= $150, |Δ| <= 0.28)
    # ------------------------------------------------------------------
    bps_regime_ok = (
        jlaw_cls in {"valid", "watch"}
        and (bundle.jlaw_score is None or bundle.jlaw_score >= 8)
        and verdict in {"bullish", "mixed"}
        and ivr >= 30.0
        and bundle.dp_accumulating is not False
        and safety_floor is not None
    )
    if bps_regime_ok and safety_floor is not None:
        target_width = 5.0 if spot < 150.0 else 10.0
        puts_by_exp: dict[str, list[OptionContractQuote]] = {}
        for p in puts:
            if 14 <= p.dte <= 35:
                puts_by_exp.setdefault(p.expiry, []).append(p)

        best_bps_row: dict[str, Any] | None = None
        for exp, exp_puts in puts_by_exp.items():
            if not is_earnings_clear(report_date, date.fromisoformat(exp), val.earnings_date, val.earnings_report_time):
                continue
            by_strike = {round(p.strike, 2): p for p in exp_puts}
            for sp in exp_puts:
                if sp.delta is None or not (0.18 <= abs(sp.delta) <= 0.28):
                    continue
                if sp.strike > safety_floor:
                    continue
                lp = by_strike.get(round(sp.strike - target_width, 2))
                if not lp or lp.ask <= 0:
                    continue
                net_credit = sp.bid - lp.ask
                if net_credit <= 0:
                    continue
                max_loss = target_width - net_credit
                if max_loss <= 0:
                    continue
                ror = net_credit / max_loss
                if not (0.20 <= ror <= 0.35):
                    continue
                adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "put", sp.strike)
                if adv_sweeps > 0:
                    continue
                ann_pct = ror * (365.0 / sp.dte) * 100.0
                row = _make_opportunity_row(
                    report_date=report_date,
                    run_id=run_id,
                    strategy="BULL_PUT_SPREAD",
                    dte_bucket="CORE",
                    bundle=bundle,
                    short_leg=sp,
                    leg2=lp,
                    leg2_role="LONG_PROTECTION",
                    spread_width=target_width,
                    net_entry_price=net_credit,
                    entry_credit=net_credit,
                    entry_debit=None,
                    collateral=target_width,
                    max_loss=max_loss,
                    return_on_risk=ror,
                    annualized_pct=ann_pct,
                    break_even_low=sp.strike - net_credit,
                    break_even_high=None,
                    safety_floor=safety_floor,
                    safety_ceiling=safety_ceiling,
                    adverse_sweeps=adv_sweeps,
                    confirming_bids=whale_bids,
                    notes=f"{int(target_width)}-wide Bull Put Spread <= floor {safety_floor:.2f} (RoR {ror*100:.1f}%)",
                )
                if not best_bps_row or row["confidence_score"] > best_bps_row["confidence_score"]:
                    best_bps_row = row
        if best_bps_row:
            results.append(best_bps_row)

    # ------------------------------------------------------------------
    # 4. SHORT_STRANGLE (28-45 DTE in Positive GEX + IV Rank >= 45%)
    # ------------------------------------------------------------------
    corridor_ok = (
        bundle.gex_regime == "positive"
        and bundle.gex_put_wall is not None
        and bundle.gex_call_wall is not None
        and bundle.gex_call_wall > bundle.gex_put_wall
        and ((bundle.gex_call_wall - bundle.gex_put_wall) / spot) >= 0.08
    )
    strangle_regime_ok = (
        corridor_ok
        and ivr >= 45.0
        and 38.0 <= rsi <= 62.0
        and verdict in {"mixed", "no-data"}
    )
    if strangle_regime_ok and bundle.gex_put_wall and bundle.gex_call_wall:
        put_floor = min(
            [x for x in [bundle.gex_put_wall, bundle.dp_top_level if bundle.dp_accumulating else None] if x]
        )
        call_ceiling = bundle.gex_call_wall
        expiries = sorted({p.expiry for p in puts if 28 <= p.dte <= 45})
        best_strangle_row: dict[str, Any] | None = None
        for exp in expiries:
            if not is_earnings_clear(report_date, date.fromisoformat(exp), val.earnings_date, val.earnings_report_time):
                continue
            exp_puts = [
                p for p in puts
                if p.expiry == exp
                and p.delta is not None
                and 0.12 <= abs(p.delta) <= 0.20
                and p.strike <= put_floor
            ]
            exp_calls = [
                c for c in calls
                if c.expiry == exp
                and c.delta is not None
                and 0.12 <= abs(c.delta) <= 0.20
                and c.strike >= call_ceiling
            ]
            if not exp_puts or not exp_calls:
                continue
            sp = max(exp_puts, key=lambda x: x.bid)
            sc = max(exp_calls, key=lambda x: x.bid)
            put_adv, put_bids = evaluate_strike_flow_prints(trades, "put", sp.strike)
            call_adv, call_bids = evaluate_strike_flow_prints(trades, "call", sc.strike)
            if put_adv + call_adv > 0:
                continue
            total_credit = sp.bid + sc.bid
            corridor_width = sc.strike - sp.strike
            approx_bpr = max(sp.strike * 0.20, sc.strike * 0.20)
            ann_pct = (total_credit / approx_bpr) * (365.0 / sp.dte) * 100.0
            if ann_pct < 18.0:
                continue
            ror = total_credit / approx_bpr
            row = _make_opportunity_row(
                report_date=report_date,
                run_id=run_id,
                strategy="SHORT_STRANGLE",
                dte_bucket="STRANGLE",
                bundle=bundle,
                short_leg=sp,
                leg2=sc,
                leg2_role="SHORT_STRANGLE_CALL",
                spread_width=corridor_width,
                net_entry_price=total_credit,
                entry_credit=total_credit,
                entry_debit=None,
                collateral=approx_bpr,
                max_loss=approx_bpr - total_credit,
                return_on_risk=ror,
                annualized_pct=ann_pct,
                break_even_low=sp.strike - total_credit,
                break_even_high=sc.strike + total_credit,
                safety_floor=put_floor,
                safety_ceiling=call_ceiling,
                adverse_sweeps=put_adv + call_adv,
                confirming_bids=put_bids + call_bids,
                notes=(
                    f"Positive-GEX Short Strangle outside [{put_floor:.2f}, {call_ceiling:.2f}] "
                    f"(IVR {ivr:.1f}%)"
                ),
            )
            if not best_strangle_row or row["confidence_score"] > best_strangle_row["confidence_score"]:
                best_strangle_row = row
        if best_strangle_row:
            results.append(best_strangle_row)

    # ------------------------------------------------------------------
    # 5. DIAGONAL_SPREAD (Bull Call PMCC Diagonal in Low IV < 38%)
    # ------------------------------------------------------------------
    diagonal_regime_ok = (
        (jlaw_cls == "valid" or (jlaw_cls == "watch" and (bundle.jlaw_score or 0) >= 11))
        and verdict == "bullish"
        and bundle.dp_accumulating is True
        and ivr < 38.0
        and term_30_60 <= 1.08
    )
    if diagonal_regime_ok:
        back_calls = [
            c for c in calls
            if 60 <= c.dte <= 95
            and c.delta is not None
            and 0.70 <= abs(c.delta) <= 0.82
            and c.strike < spot
            and c.ask > 0
        ]
        front_Target_ceiling = safety_ceiling or round(spot * 1.03, 2)
        front_calls = [
            c for c in calls
            if 7 <= c.dte <= 21
            and c.delta is not None
            and 0.20 <= abs(c.delta) <= 0.30
            and c.strike >= front_Target_ceiling
            and is_earnings_clear(report_date, date.fromisoformat(c.expiry), val.earnings_date, val.earnings_report_time)
        ]
        best_diag_row: dict[str, Any] | None = None
        for bc in back_calls:
            intrinsic = max(0.0, spot - bc.strike)
            extrinsic = max(0.0, bc.ask - intrinsic)
            if bc.ask <= 0 or (extrinsic / bc.ask) > 0.25:
                continue
            for fc in front_calls:
                if fc.strike <= bc.strike:
                    continue
                strike_width = fc.strike - bc.strike
                net_debit = bc.ask - fc.bid
                if net_debit <= 0:
                    continue
                # Hard No-Upside-Inversion Gate: net debit <= 78% of strike width
                if net_debit > 0.78 * strike_width:
                    continue
                adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "call", fc.strike)
                if adv_sweeps > 0:
                    continue
                cycle_ror = fc.bid / net_debit
                ann_pct = cycle_ror * (365.0 / fc.dte) * 100.0
                row = _make_opportunity_row(
                    report_date=report_date,
                    run_id=run_id,
                    strategy="DIAGONAL_SPREAD",
                    dte_bucket="DIAGONAL",
                    bundle=bundle,
                    short_leg=fc,
                    leg2=bc,
                    leg2_role="LONG_DIAGONAL_BACK",
                    spread_width=strike_width,
                    net_entry_price=-net_debit,
                    entry_credit=None,
                    entry_debit=net_debit,
                    collateral=net_debit,
                    max_loss=net_debit,
                    return_on_risk=cycle_ror,
                    annualized_pct=ann_pct,
                    break_even_low=bc.strike + net_debit,
                    break_even_high=None,
                    safety_floor=safety_floor,
                    safety_ceiling=front_Target_ceiling,
                    adverse_sweeps=adv_sweeps,
                    confirming_bids=whale_bids,
                    notes=(
                        f"Low-IV Bull Call Diagonal: Long {bc.expiry} {bc.strike:.0f}C / "
                        f"Short {fc.expiry} {fc.strike:.0f}C (debit {net_debit:.2f} <= 78% width)"
                    ),
                )
                if not best_diag_row or row["confidence_score"] > best_diag_row["confidence_score"]:
                    best_diag_row = row
        if best_diag_row:
            results.append(best_diag_row)

    # ------------------------------------------------------------------
    # 6. BEAR_CALL_SPREAD (Bearish flow + DP distribution call vertical)
    # ------------------------------------------------------------------
    bcs_regime_ok = (
        verdict == "bearish"
        and ((bundle.bearish_z or 0.0) >= 1.0 or bundle.dp_accumulating is False)
        and ivr >= 30.0
        and safety_ceiling is not None
    )
    if bcs_regime_ok and safety_ceiling is not None:
        target_width = 5.0 if spot < 150.0 else 10.0
        calls_by_exp: dict[str, list[OptionContractQuote]] = {}
        for c in calls:
            if 14 <= c.dte <= 35:
                calls_by_exp.setdefault(c.expiry, []).append(c)

        best_bcs_row: dict[str, Any] | None = None
        for exp, exp_calls in calls_by_exp.items():
            if not is_earnings_clear(report_date, date.fromisoformat(exp), val.earnings_date, val.earnings_report_time):
                continue
            by_strike = {round(c.strike, 2): c for c in exp_calls}
            for sc in exp_calls:
                if sc.delta is None or not (0.18 <= abs(sc.delta) <= 0.28):
                    continue
                if sc.strike < safety_ceiling:
                    continue
                lc = by_strike.get(round(sc.strike + target_width, 2))
                if not lc or lc.ask <= 0:
                    continue
                net_credit = sc.bid - lc.ask
                if net_credit <= 0:
                    continue
                max_loss = target_width - net_credit
                if max_loss <= 0:
                    continue
                ror = net_credit / max_loss
                if ror < 0.20:
                    continue
                adv_sweeps, whale_bids = evaluate_strike_flow_prints(trades, "call", sc.strike)
                if adv_sweeps > 0:
                    continue
                ann_pct = ror * (365.0 / sc.dte) * 100.0
                row = _make_opportunity_row(
                    report_date=report_date,
                    run_id=run_id,
                    strategy="BEAR_CALL_SPREAD",
                    dte_bucket="CORE",
                    bundle=bundle,
                    short_leg=sc,
                    leg2=lc,
                    leg2_role="LONG_PROTECTION",
                    spread_width=target_width,
                    net_entry_price=net_credit,
                    entry_credit=net_credit,
                    entry_debit=None,
                    collateral=target_width,
                    max_loss=max_loss,
                    return_on_risk=ror,
                    annualized_pct=ann_pct,
                    break_even_low=None,
                    break_even_high=sc.strike + net_credit,
                    safety_floor=safety_floor,
                    safety_ceiling=safety_ceiling,
                    adverse_sweeps=adv_sweeps,
                    confirming_bids=whale_bids,
                    notes=f"{int(target_width)}-wide Bear Call Spread >= ceiling {safety_ceiling:.2f}",
                )
                if not best_bcs_row or row["confidence_score"] > best_bcs_row["confidence_score"]:
                    best_bcs_row = row
        if best_bcs_row:
            results.append(best_bcs_row)

    results.sort(key=lambda r: r["confidence_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Automated Lifecycle Evaluation (50% Profit, 21-DTE Mark, GEX Breach, Expiry)
# ---------------------------------------------------------------------------


def evaluate_open_opportunity_row(
    row: dict[str, Any],
    as_of_date: date,
    spot_price: float | None,
    short_quote: OptionContractQuote | None,
    leg2_quote: OptionContractQuote | None = None,
    current_gex_put_wall: float | None = None,
    current_gex_call_wall: float | None = None,
) -> dict[str, Any]:
    """
    Evaluates an existing `PROPOSED` or `ACTIVE` row against current NBBO quotes
    and GEX walls:
      1. 50% Max-Profit Auto-Win (`outcome = 'WON'`, `win = True`, `status = 'CLOSED'`)
      2. 21-DTE Davis Decision Checkpoint (`dte21_mark_date`, `dte21_close_mark`, `dte21_pnl`)
      3. Live GEX Wall Breach Alert (`defense_trigger_date`, `defense_reason`)
      4. Expiry Intrinsic Settlement (`EXPIRED_WORTHLESS` or `LOST`)
    """
    updated = dict(row)
    if updated.get("outcome") and updated.get("win") is not None:
        return updated

    strategy = str(updated.get("strategy") or "")
    exp_date = parse_iso_date(updated.get("short_expiry"))
    if not exp_date:
        return updated

    dte_now = (exp_date - as_of_date).days
    entry_credit = safe_float(updated.get("entry_credit"))
    entry_debit = safe_float(updated.get("entry_debit"))
    short_strike = safe_float(updated.get("short_strike")) or 0.0
    leg2_strike = safe_float(updated.get("leg2_strike"))

    # Compute conservative NBBO closing mark
    close_mark: float | None = None
    if short_quote is not None:
        if strategy in {"WHEEL_CSP", "WHEEL_CC"}:
            close_mark = short_quote.ask
        elif strategy in {"BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"} and leg2_quote is not None:
            close_mark = max(0.0, short_quote.ask - leg2_quote.bid)
        elif strategy == "SHORT_STRANGLE" and leg2_quote is not None:
            close_mark = short_quote.ask + leg2_quote.ask
        elif strategy == "DIAGONAL_SPREAD" and leg2_quote is not None:
            # Spread value if liquidated today = back_long_bid - front_short_ask
            close_mark = max(0.0, leg2_quote.bid - short_quote.ask)

    # 1. Check 50% Profit Auto-Win before expiry
    if close_mark is not None and dte_now > 0:
        if strategy != "DIAGONAL_SPREAD" and entry_credit and entry_credit > 0:
            if close_mark <= 0.50 * entry_credit:
                pnl = round(entry_credit - close_mark, 4)
                updated["profit50_hit_date"] = as_of_date.isoformat()
                updated["profit50_close_mark"] = round(close_mark, 4)
                updated["status"] = "CLOSED"
                updated["outcome"] = "WON"
                updated["win"] = True
                updated["exit_date"] = as_of_date.isoformat()
                updated["exit_close_mark"] = round(close_mark, 4)
                updated["exit_pnl"] = pnl
                updated["outcome_notes"] = f"Hit >=50% max profit on {as_of_date.isoformat()} (mark {close_mark:.2f})"
                return updated
        elif strategy == "DIAGONAL_SPREAD" and entry_debit and entry_debit > 0:
            if close_mark >= 1.25 * entry_debit:
                pnl = round(close_mark - entry_debit, 4)
                updated["profit50_hit_date"] = as_of_date.isoformat()
                updated["profit50_close_mark"] = round(close_mark, 4)
                updated["status"] = "CLOSED"
                updated["outcome"] = "WON"
                updated["win"] = True
                updated["exit_date"] = as_of_date.isoformat()
                updated["exit_close_mark"] = round(close_mark, 4)
                updated["exit_pnl"] = pnl
                updated["outcome_notes"] = f"Hit +25% diagonal return on {as_of_date.isoformat()}"
                return updated

    # 2. Record 21-DTE Davis Decision Checkpoint
    if 0 < dte_now <= 21 and not updated.get("dte21_mark_date") and close_mark is not None:
        updated["dte21_mark_date"] = as_of_date.isoformat()
        updated["dte21_close_mark"] = round(close_mark, 4)
        if strategy != "DIAGONAL_SPREAD" and entry_credit is not None:
            updated["dte21_pnl"] = round(entry_credit - close_mark, 4)
        elif strategy == "DIAGONAL_SPREAD" and entry_debit is not None:
            updated["dte21_pnl"] = round(close_mark - entry_debit, 4)

    # 3. Live GEX Wall Breach Alert
    if dte_now > 0 and not updated.get("defense_trigger_date"):
        if (
            strategy in {"WHEEL_CSP", "BULL_PUT_SPREAD", "SHORT_STRANGLE"}
            and current_gex_put_wall is not None
            and short_strike > 0
            and current_gex_put_wall < short_strike
        ):
            updated["defense_trigger_date"] = as_of_date.isoformat()
            updated["defense_reason"] = "GEX_PUT_WALL_BREACH"
        elif (
            strategy in {"WHEEL_CC", "BEAR_CALL_SPREAD", "SHORT_STRANGLE"}
            and current_gex_call_wall is not None
            and short_strike > 0
            and current_gex_call_wall > short_strike
            and spot_price is not None
            and spot_price >= short_strike
        ):
            updated["defense_trigger_date"] = as_of_date.isoformat()
            updated["defense_reason"] = "GEX_CALL_WALL_BREACH"

    # 4. Expiry Intrinsic Settlement
    if dte_now <= 0 and spot_price is not None:
        if strategy == "WHEEL_CSP":
            intrinsic = max(0.0, short_strike - spot_price)
            pnl = (entry_credit or 0.0) - intrinsic
        elif strategy == "WHEEL_CC":
            intrinsic = max(0.0, spot_price - short_strike)
            pnl = (entry_credit or 0.0) - intrinsic
        elif strategy == "BULL_PUT_SPREAD":
            long_k = leg2_strike or (short_strike - (safe_float(updated.get("spread_width")) or 5.0))
            intrinsic = max(0.0, short_strike - spot_price) - max(0.0, long_k - spot_price)
            pnl = (entry_credit or 0.0) - intrinsic
        elif strategy == "BEAR_CALL_SPREAD":
            long_k = leg2_strike or (short_strike + (safe_float(updated.get("spread_width")) or 5.0))
            intrinsic = max(0.0, spot_price - short_strike) - max(0.0, spot_price - long_k)
            pnl = (entry_credit or 0.0) - intrinsic
        elif strategy == "SHORT_STRANGLE":
            call_k = leg2_strike or (short_strike * 1.10)
            intrinsic = max(0.0, short_strike - spot_price) + max(0.0, spot_price - call_k)
            pnl = (entry_credit or 0.0) - intrinsic
        else:
            # DIAGONAL_SPREAD front expiry
            intrinsic = close_mark if close_mark is not None else (entry_debit or 0.0)
            pnl = intrinsic - (entry_debit or 0.0)

        updated["status"] = "CLOSED"
        updated["exit_date"] = as_of_date.isoformat()
        updated["exit_close_mark"] = round(max(0.0, intrinsic), 4)
        updated["exit_pnl"] = round(pnl, 4)
        if pnl >= 0:
            updated["outcome"] = "EXPIRED_WORTHLESS" if intrinsic == 0 else "WON"
            updated["win"] = True
        else:
            updated["outcome"] = "LOST"
            updated["win"] = False
        updated["outcome_notes"] = f"Settled at expiry ({exp_date.isoformat()}) with spot {spot_price:.2f}"

    return updated


# ---------------------------------------------------------------------------
# External Clients (Unusual Whales API & Supabase PostgREST)
# ---------------------------------------------------------------------------


class UnusualWhalesClient:
    """
    Strictly adheres to the user's `unusual-whales-api` skill rules:
      - Base URL: https://api.unusualwhales.com
      - Header: Authorization: Bearer <API_TOKEN>
      - Header: UW-CLIENT-API-ID: 100001
      - Method: GET only
    """

    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "UW-CLIENT-API-ID": UW_CLIENT_API_ID,
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{UW_BASE_URL}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code in {403, 404, 422}:
            return None
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get_earnings(self, ticker: str) -> list[dict[str, Any]]:
        res = self._get(f"/api/stock/{ticker}/earnings")
        return res if isinstance(res, list) else []

    def get_financials(self, ticker: str) -> dict[str, Any] | list[dict[str, Any]]:
        res = self._get(f"/api/stock/{ticker}/financials")
        return res if isinstance(res, (dict, list)) else {}

    def get_interpolated_iv(self, ticker: str, date_str: str | None = None) -> list[dict[str, Any]]:
        params = {"date": date_str} if date_str else None
        res = self._get(f"/api/stock/{ticker}/interpolated-iv", params=params)
        return res if isinstance(res, list) else []

    def get_rsi_14(self, ticker: str) -> list[dict[str, Any]]:
        res = self._get(
            f"/api/stock/{ticker}/technical-indicator/RSI",
            params={"interval": "daily", "time_period": 14, "series_type": "close"},
        )
        return res if isinstance(res, list) else []

    def _fetch_contract_rows(self, ticker: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self._get(f"/api/stock/{ticker}/option-contracts", params=params)
        return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []

    def get_option_contracts(
        self,
        ticker: str,
        as_of_date: date,
        expiry: str | None = None,
        min_dte: int = 3,
        max_dte: int = 95,
    ) -> list[OptionContractQuote]:
        """
        `/api/stock/{ticker}/option-contracts` caps each response at 500 rows spread across all
        expiries (pagination does not extend it). So: (1) one discovery call to list expiries,
        then (2) one call per expiry inside [min_dte, max_dte] to get the full strike ladder.
        """
        base = {"exclude_zero_dte": "true"}
        if expiry:
            raw_rows = self._fetch_contract_rows(ticker, {**base, "expiry": expiry})
        else:
            discovery = self._fetch_contract_rows(ticker, base)
            expiries: set[str] = set()
            for row in discovery:
                parsed = parse_occ_symbol(str(row.get("option_symbol") or ""))
                if parsed:
                    dte = (date.fromisoformat(parsed["expiry"]) - as_of_date).days
                    if min_dte <= dte <= max_dte:
                        expiries.add(parsed["expiry"])
            merged: dict[str, dict[str, Any]] = {str(x.get("option_symbol")): x for x in discovery}
            for exp in sorted(expiries):
                for row in self._fetch_contract_rows(ticker, {**base, "expiry": exp}):
                    merged[str(row.get("option_symbol"))] = row
            raw_rows = list(merged.values())

        quotes: list[OptionContractQuote] = []
        for r in raw_rows:
            occ = str(r.get("option_symbol") or "")
            parsed = parse_occ_symbol(occ)
            if not parsed:
                continue
            exp_date = parse_iso_date(parsed["expiry"])
            if not exp_date:
                continue
            dte = (exp_date - as_of_date).days
            if dte < 0:
                continue
            quotes.append(
                OptionContractQuote(
                    option_symbol=occ,
                    underlying_symbol=parsed["underlying_symbol"],
                    option_type=parsed["option_type"],
                    expiry=parsed["expiry"],
                    dte=dte,
                    strike=parsed["strike"],
                    bid=safe_float(r.get("nbbo_bid")) or 0.0,
                    ask=safe_float(r.get("nbbo_ask")) or 0.0,
                    delta=safe_float(r.get("delta")),
                    iv=safe_float(r.get("implied_volatility")),
                    open_interest=safe_float(r.get("open_interest")) or 0.0,
                    volume=safe_float(r.get("volume")) or 0.0,
                    sweep_volume=safe_float(r.get("sweep_volume")) or 0.0,
                    ask_volume=safe_float(r.get("ask_volume")) or 0.0,
                    bid_volume=safe_float(r.get("bid_volume")) or 0.0,
                )
            )
        return quotes

    def get_option_trades(self, ticker: str, limit: int = 150) -> list[dict[str, Any]]:
        res = self._get("/api/option-trades", params={"ticker_symbol": ticker, "limit": limit})
        return res if isinstance(res, list) else []


class SupabaseRestClient:
    """
    Lightweight PostgREST wrapper for reading upstream signal tables and
    idempotently merging rows into `public."uw-option-opportunities"`.
    """

    def __init__(self, url: str, key: str, timeout: int = 20):
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
        )

    def select(self, table: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        resp = self.session.get(f"{self.base_url}/{table}", params=params, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def patch_row(self, table: str, opportunity_key: str, changes: dict[str, Any]) -> None:
        changes = dict(changes)
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        resp = self.session.patch(
            f"{self.base_url}/{table}",
            params={"opportunity_key": f"eq.{opportunity_key}"},
            data=json.dumps(changes, default=str),
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def upsert_opportunity(self, table: str, row: dict[str, Any]) -> None:
        """
        Idempotent PATCH-then-INSERT by `opportunity_key` while preserving
        terminal outcome/win fields if already settled.
        """
        opp_key = row["opportunity_key"]
        existing = self.select(
            table,
            params={
                "select": "opportunity_key,status,outcome,win,profit50_hit_date,dte21_mark_date,exit_date",
                "opportunity_key": f"eq.{opp_key}",
            },
        )
        payload = dict(row)
        if existing:
            ex = existing[0]
            if ex.get("outcome") and ex.get("win") is not None:
                for k in ("status", "outcome", "win", "profit50_hit_date", "dte21_mark_date", "exit_date"):
                    if ex.get(k) is not None:
                        payload[k] = ex[k]
            resp = self.session.patch(
                f"{self.base_url}/{table}",
                params={"opportunity_key": f"eq.{opp_key}"},
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            resp.raise_for_status()
        else:
            resp = self.session.post(
                f"{self.base_url}/{table}",
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Markdown Report Formatter
# ---------------------------------------------------------------------------


def render_markdown_report(
    report_date: date,
    opportunities: list[dict[str, Any]],
    lifecycle_updates: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# UW Stateless Options Income Report — {report_date.isoformat()}")
    lines.append("")
    lines.append(
        "Stateless multi-strategy scan combining J Law structure, Unusual Whales flow verdicts, "
        "GEX walls, Dark Pool accumulation, and UW fundamental/IV/RSI gates."
    )
    lines.append("")

    # Summary counts by strategy
    by_strat: dict[str, list[dict[str, Any]]] = {}
    for row in opportunities:
        by_strat.setdefault(row["strategy"], []).append(row)

    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(f"- **Total Qualified Opportunities (`>= 60` Score):** {len(opportunities)}")
    for strat in (
        "WHEEL_CSP",
        "WHEEL_CC",
        "BULL_PUT_SPREAD",
        "SHORT_STRANGLE",
        "DIAGONAL_SPREAD",
        "BEAR_CALL_SPREAD",
    ):
        rows = by_strat.get(strat, [])
        lines.append(f"- **`{strat}`:** {len(rows)} qualified setup(s)")
    lines.append("")

    # Top Ranked Table
    lines.append("## 2. Ranked Opportunity Board")
    lines.append("")
    if not opportunities:
        lines.append("No symbols passed all mechanical regime, support-floor, and NBBO economics gates today.")
    else:
        lines.append(
            "| Rank | Symbol | Strategy | Bucket | Expiry (DTE) | Primary Leg | Leg 2 | Net Credit/Debit | RoR / Ann% | Floor / Ceiling | UW Valuation | IVR / RSI | Score (Band) |"
        )
        lines.append("|---:|---|---|---|---|---|---|---:|---:|---|---|---|---:|")
        for idx, r in enumerate(opportunities, start=1):
            leg1_str = f"{r['short_type'].upper()} {r['short_strike']:.1f} (Δ {r['short_delta'] or 0:.2f})"
            leg2_str = (
                f"{(r['leg2_type'] or '').upper()} {r['leg2_strike']:.1f}"
                if r.get("leg2_strike") is not None
                else "—"
            )
            net_p = r.get("net_entry_price") or 0.0
            price_label = f"+${net_p:.2f} cr" if net_p >= 0 else f"-${abs(net_p):.2f} db"
            ror_pct = (r.get("return_on_risk") or 0.0) * 100.0
            ann_pct = r.get("annualized_pct") or 0.0
            floor_s = f"{r['safety_floor_price']:.2f}" if r.get("safety_floor_price") else "—"
            ceil_s = f"{r['safety_ceiling_price']:.2f}" if r.get("safety_ceiling_price") else "—"
            ivr_s = f"{r['uw_iv_rank']:.0f}%" if r.get("uw_iv_rank") is not None else "—"
            rsi_s = f"{r['uw_rsi_14']:.0f}" if r.get("uw_rsi_14") is not None else "—"
            lines.append(
                f"| {idx} | **{r['symbol']}** | `{r['strategy']}` | `{r['dte_bucket']}` | "
                f"{r['short_expiry']} ({r['short_dte']}d) | {leg1_str} | {leg2_str} | "
                f"{price_label} | {ror_pct:.1f}% / {ann_pct:.1f}% | {floor_s} / {ceil_s} | "
                f"`{r.get('uw_valuation_tier')}` | {ivr_s} / {rsi_s} | **{r['confidence_score']:.1f}** (`{r['score_band']}`) |"
            )
    lines.append("")

    if lifecycle_updates:
        lines.append("## 3. Open Ledger Lifecycle & Defense Updates")
        lines.append("")
        for u in lifecycle_updates:
            lines.append(
                f"- **{u.get('symbol')}** (`{u.get('strategy')}` {u.get('short_expiry')} {u.get('short_strike')}): "
                f"status=`{u.get('status')}`, outcome=`{u.get('outcome')}`, defense=`{u.get('defense_reason')}` "
                f"({u.get('outcome_notes') or 'checked'})"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


WATCHLIST_CATEGORIES: tuple[str, ...] = ("core", "satellite", "watch1")
US_OPTION_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "ARCA", "BATS", "CBOE"})
_CATEGORY_PRIORITY = {c: i for i, c in enumerate(WATCHLIST_CATEGORIES)}


def build_watchlist_universe(
    watchlist_rows: list[dict[str, Any]],
    symbol_override: list[str] | None = None,
) -> list[tuple[str, str]]:
    """
    Builds the scan universe strictly from `public.watchlist`:
      - Only `category` in {core, satellite, watch1} (case/whitespace-insensitive).
      - Only US-listed exchanges (drops LSE / OTC / Stockholm / XETR, e.g. BESIY, IQE.L).
      - De-duplicates symbols, keeping the highest-priority category (core > satellite > watch1).
      - `symbol_override` (from --symbols) can only NARROW this universe; tickers that are not
        in core/satellite/watch1 are ignored.
    Never reads holding/hedge columns (only symbol, category, exchange are selected).
    """
    best: dict[str, str] = {}
    for row in watchlist_rows or []:
        sym = str(row.get("symbol") or "").strip().upper()
        cat = str(row.get("category") or "").strip().lower()
        exch = str(row.get("exchange") or "").strip().upper()
        if cat not in _CATEGORY_PRIORITY:
            continue
        if exch and exch not in US_OPTION_EXCHANGES:
            continue
        if not is_us_option_symbol(sym):
            continue
        if sym not in best or _CATEGORY_PRIORITY[cat] < _CATEGORY_PRIORITY[best[sym]]:
            best[sym] = cat

    if symbol_override:
        wanted = {s.strip().upper() for s in symbol_override if s and s.strip()}
        best = {s: c for s, c in best.items() if s in wanted}

    return sorted(best.items(), key=lambda kv: (_CATEGORY_PRIORITY[kv[1]], kv[0]))


def us_market_date() -> date:
    """Today's date in US/Eastern (option DTE and UW data are keyed to the US session)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # pragma: no cover
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stateless UW Multi-Strategy Options Income Engine")
    p.add_argument("--date", default=us_market_date().isoformat(), help="US/Eastern report date (YYYY-MM-DD)")
    p.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated subset (e.g. NVDA,AVGO). Only tickers already in core/satellite/watch1 are scanned.",
    )
    p.add_argument("--min-score", type=float, default=60.0, help="Minimum confidence score to publish (default 60)")
    p.add_argument("--top-n", type=int, default=20, help="Max ranked opportunities to publish (default 20)")
    p.add_argument("--table", default="uw-option-opportunities", help="Supabase destination table")
    p.add_argument("--dry-run", action="store_true", help="Do not write to Supabase")
    p.add_argument("--skip-tracking", action="store_true", help="Skip the open-ledger lifecycle pass")
    p.add_argument("--output-dir", default="reports", help="Directory for Markdown/JSON output artifacts")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_date = date.fromisoformat(args.date)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    uw_key = os.environ.get("UW_API_KEY") or os.environ.get("UNUSUAL_WHALES_API_KEY") or ""
    sb_url = os.environ.get("SUPABASE_URL") or ""
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or ""

    if not uw_key:
        print("ERROR: Missing UW_API_KEY environment variable.", file=sys.stderr)
        return 2

    uw = UnusualWhalesClient(api_key=uw_key)
    sb = SupabaseRestClient(url=sb_url, key=sb_key) if (sb_url and sb_key) else None
    run_id = f"uw-income-{report_date.isoformat()}-{int(time.time())}"

    # Universe = Supabase watchlist, restricted to core / satellite / watch1 (US-listed only)
    if sb is None:
        print(
            "ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required; the universe is "
            "loaded from public.watchlist (core/satellite/watch1).",
            file=sys.stderr,
        )
        return 2
    wl_rows = sb.select(
        "watchlist",
        params={
            "select": "symbol,category,exchange",
            "category": f"in.({','.join(WATCHLIST_CATEGORIES)})",
        },
    )
    override = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    symbols = build_watchlist_universe(wl_rows, override)
    print(f"Universe: {len(symbols)} watchlist symbols ({', '.join(WATCHLIST_CATEGORIES)})")

    all_candidates: list[dict[str, Any]] = []
    chain_cache: dict[str, list[OptionContractQuote]] = {}
    spot_cache: dict[str, float] = {}
    gex_cache: dict[str, tuple[float | None, float | None]] = {}
    for sym, cat in symbols:
        earnings_rows = uw.get_earnings(sym)
        iv_rows = uw.get_interpolated_iv(sym)  # latest US session
        rsi_rows = uw.get_rsi_14(sym)
        vol_ctx = compute_uw_volatility_context(sym, iv_rows, rsi_rows=rsi_rows)

        bundle = SymbolSignalBundle(
            symbol=sym,
            watchlist_category=cat,
            spot_price=None,
            quote_as_of=datetime.now(timezone.utc).isoformat(),
            quote_status="live",
            valuation=None,
            volatility=vol_ctx,
        )

        # Enrich from Supabase upstream tables when available
        if sb is not None:
            jlaw = sb.select(
                "trade-jlaw-v2",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "run_date.desc", "limit": "1"},
            )
            if jlaw:
                jr = jlaw[0]
                bundle.jlaw_run_date = jr.get("run_date")
                bundle.jlaw_classification = jr.get("classification")
                bundle.jlaw_score = safe_int(jr.get("jlaw_score") or jr.get("total_score"))
                bundle.jlaw_stop_loss = safe_float(jr.get("stop_loss"))
                bundle.jlaw_target_price = safe_float(jr.get("target_price"))
                bundle.option_score = safe_int(jr.get("option_score"))
                bundle.option_tier = jr.get("option_tier")
                bundle.option_alert = jr.get("option_alert")
                if not bundle.spot_price:
                    bundle.spot_price = safe_float(jr.get("current_price"))
                if vol_ctx.iv_rank is None:
                    vol_ctx.iv_rank = safe_float(jr.get("option_iv_rank"))

            gex = sb.select(
                "gex_snapshot",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "snapshot_ts.desc", "limit": "1"},
            )
            if gex:
                gr = gex[0]
                bundle.gex_snapshot_ts = gr.get("snapshot_ts")
                bundle.gex_regime = str(gr.get("regime") or "sub_resolution")
                bundle.gex_put_wall = safe_float(gr.get("put_wall"))
                bundle.gex_call_wall = safe_float(gr.get("call_wall"))
                bundle.gex_net_mm = safe_float(gr.get("net_gex_mm"))
                bundle.gex_gravity = gr.get("gravity")
                if not bundle.spot_price:
                    bundle.spot_price = safe_float(gr.get("spot_price"))

            fv = sb.select(
                "flow_verdict_rows",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "verdict_date.desc", "limit": "1"},
            )
            next_earn_override: str | None = None
            if fv:
                fr = fv[0]
                bundle.verdict = str(fr.get("verdict") or "no-data")
                bundle.verdict_reasons = list(fr.get("reasons") or fr.get("verdict_reasons") or []) + list(fr.get("caveats") or [])
                bundle.key_flows = fr.get("key_flows") or []
                if fr.get("next_earnings_date"):
                    next_earn_override = str(fr.get("next_earnings_date"))[:10]

            flow_sig = sb.select(
                "option_flow_symbol_signals",
                params={"select": "*", "ticker": f"eq.{sym}", "order": "signal_date.desc", "limit": "1"},
            )
            if flow_sig:
                fs = flow_sig[0]
                bundle.flow_signal_date = fs.get("signal_date")
                bundle.flow_bias = fs.get("net_option_flow_bias")
                bundle.bullish_z = safe_float(fs.get("bullish_z"))
                bundle.bearish_z = safe_float(fs.get("bearish_z"))
                bundle.net_bias_z = safe_float(fs.get("net_bias_z"))

            dp = sb.select(
                "darkpool_symbol_daily",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "fetch_date.desc", "limit": "1"},
            )
            if dp:
                dr = dp[0]
                bundle.dp_as_of = dr.get("window_end") or dr.get("fetch_date") or dr.get("as_of_date")
                bundle.dp_total_premium_mm = round((safe_float(dr.get("total_premium")) or 0.0) / 1e6, 2)
                bundle.dp_buy_mm = round((safe_float(dr.get("buy_premium")) or 0.0) / 1e6, 2)
                bundle.dp_sell_mm = round((safe_float(dr.get("sell_premium")) or 0.0) / 1e6, 2)
                bundle.dp_net_mm = round((safe_float(dr.get("net_premium")) or 0.0) / 1e6, 2)
                bundle.dp_blocks = safe_int(dr.get("block_count"))
                bundle.dp_top_level = safe_float(dr.get("top_level"))
                bundle.dp_top_level_mm = round((safe_float(dr.get("top_level_premium")) or 0.0) / 1e6, 2)
                bundle.dp_accumulating = bool(dr.get("accumulating")) if dr.get("accumulating") is not None else None
                if not bundle.spot_price and bundle.dp_top_level:
                    bundle.spot_price = bundle.dp_top_level
        else:
            next_earn_override = None

        trades = uw.get_option_trades(sym, limit=120)
        if not bundle.spot_price and trades:
            for t in trades:
                u_px = safe_float(t.get("underlying_price"))
                if u_px and u_px > 0:
                    bundle.spot_price = u_px
                    break

        if not bundle.spot_price:
            continue

        val_ctx = compute_uw_valuation_context(sym, bundle.spot_price, earnings_rows, [], as_of_date=report_date)
        if not val_ctx.earnings_date and next_earn_override:
            val_ctx.earnings_date = next_earn_override
        bundle.valuation = val_ctx

        chain = uw.get_option_contracts(sym, as_of_date=report_date)
        chain_cache[sym] = chain
        spot_cache[sym] = bundle.spot_price
        gex_cache[sym] = (bundle.gex_put_wall, bundle.gex_call_wall)
        sym_opps = generate_opportunities_for_symbol(report_date, run_id, bundle, chain, trades)
        all_candidates.extend(sym_opps)

    # Lifecycle pass on open PROPOSED/ACTIVE rows (stateless hypothetical tracking)
    lifecycle_updates: list[dict[str, Any]] = []
    if sb is not None and not args.skip_tracking:
        open_rows = sb.select(
            args.table,
            params={"select": "*", "status": "in.(PROPOSED,ACTIVE)", "report_date": f"lt.{report_date.isoformat()}"},
        )
        for row in open_rows:
            sym = str(row.get("symbol") or "").upper()
            if sym not in chain_cache:
                chain_cache[sym] = uw.get_option_contracts(sym, as_of_date=report_date)
            by_occ = {c.option_symbol: c for c in chain_cache.get(sym, [])}
            pw, cw = gex_cache.get(sym, (None, None))
            updated = evaluate_open_opportunity_row(
                row,
                as_of_date=report_date,
                spot_price=spot_cache.get(sym),
                short_quote=by_occ.get(str(row.get("short_option_symbol") or "")),
                leg2_quote=by_occ.get(str(row.get("leg2_option_symbol") or "")),
                current_gex_put_wall=pw,
                current_gex_call_wall=cw,
            )
            changed = {k: v for k, v in updated.items() if row.get(k) != v}
            if changed:
                lifecycle_updates.append(updated)
                if not args.dry_run:
                    sb.patch_row(args.table, row["opportunity_key"], changed)

    qualified = [r for r in all_candidates if (r.get("confidence_score") or 0.0) >= args.min_score]
    qualified.sort(key=lambda r: r["confidence_score"], reverse=True)
    published = qualified[: args.top_n]

    if sb is not None and not args.dry_run:
        for row in published:
            sb.upsert_opportunity(args.table, row)

    md_text = render_markdown_report(report_date, published, lifecycle_updates)
    md_path = out_dir / f"uw_options_income_{report_date.isoformat()}.md"
    json_path = out_dir / f"uw_options_income_{report_date.isoformat()}.json"
    md_path.write_text(md_text, encoding="utf-8")
    json_path.write_text(json.dumps(published, indent=2), encoding="utf-8")
    print(f"Wrote {len(published)} qualified opportunities to {md_path} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
