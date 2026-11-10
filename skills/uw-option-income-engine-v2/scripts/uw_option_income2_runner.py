#!/usr/bin/env python3
"""
uw_option_income2_runner.py — UW Option Income Engine v2 ("uw-option-income-2")

Short-term credit-only successor to `uw-options-income-engine`:

  Strategies (4, all credit):        DTE buckets (2):
    SHORT_PUT   (cash-secured put)     LT8     1-7 DTE
    SHORT_CALL  (undefined-risk ref)   DTE8_14 8-14 DTE
    BULL_PUT_SPREAD (credit > $0.70)
    BEAR_CALL_SPREAD (credit > $0.70)

  Hard strike anchors are DTE-term-structure bucket walls computed in-engine from the
  UW option chain (gamma x OI x 100 x spot x 0.01 / 1e6 per side, nearest-expiry-per-
  strike inside the bucket) — NOT the 0-30 DTE blended walls in `gex_snapshot`, which
  are stored only as a fallback (wall_source = 'gex_blend').

  Weekly quota: 10 tickers x 2 contracts = 20 contracts, 40% single-leg / 60%
  vertical spreads, short call capped at 4, max 2 tickers per sector, market-regime
  tilt from `trade-jlaw-v2.regime`, credit projection vs the $5,000/week target.

Stateless by design: never reads brokerage holdings or `watchlist.position`.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import argparse
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any

import requests


UW_BASE_URL = "https://api.unusualwhales.com"
UW_CLIENT_API_ID = "100001"
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

# ---------------------------------------------------------------------------
# Engine Configuration
# ---------------------------------------------------------------------------

WATCHLIST_CATEGORIES = ("core", "satellite", "watch1")
ALL_WATCHLIST_CATEGORIES = ("core", "satellite", "watch1", "watch2", "watch3", "euro")

DTE_BUCKETS: dict[str, tuple[int, int]] = {
    "LT8": (1, 7),       # short-dated weeklies (and Mon/Wed expiries inside 7 days)
    "DTE8_14": (8, 14),  # two-week tenor
}

STRATEGIES = ("SHORT_PUT", "SHORT_CALL", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
SINGLE_LEG_STRATEGIES = {"SHORT_PUT", "SHORT_CALL"}
SPREAD_STRATEGIES = {"BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"}
BULL_SIDE_STRATEGIES = {"SHORT_PUT", "BULL_PUT_SPREAD"}
BEAR_SIDE_STRATEGIES = {"SHORT_CALL", "BEAR_CALL_SPREAD"}

SPREAD_MIN_CREDIT = 0.70          # strict NBBO (short_bid - long_ask) > $0.70 per share
MIN_SINGLE_CREDIT = 0.30          # per share; single legs must at least pay for slippage
SPREAD_ROR_BAND = (0.15, 0.50)    # return-on-risk window for short-DTE verticals
SINGLE_DELTA_BAND = (0.05, 0.20)
SPREAD_DELTA_BAND = (0.10, 0.28)
SINGLE_LEG_ANNUALIZED = {"LT8": 25.0, "DTE8_14": 20.0}
SINGLE_LEG_MAX_SPOT = 600.0       # no naked short put/call on tickers above $600 (collateral + gap risk)
SPREAD_WIDTH_PREFERENCE = (5.0, 10.0)       # tech sectors: prefer $5 verticals; $10 only as fallback
SPREAD_WIDTH_PREFERENCE_NONTECH = (10.0, 5.0)  # other sectors: $10-wide collects usable premium at low vol
ROR_FLOOR_BY_WIDTH = {5.0: 0.15, 10.0: 0.10}   # wider spreads run structurally lower credit/max-loss
SECTOR_RESERVE_NONTECH = 5                     # planner: at least this many planned tickers from non-tech sectors
# Sector groups: tech sectors keep the $5 spread rule; everything else (incl. new
# Biotech & Pharma / Consumer Staples names) trades $10-wide verticals.
TECH_SECTORS = {
    "Semiconductor", "Software & Cloud", "Internet & Media", "Tech Hardware",
    "Networking Equipment", "Data Center & Crypto", "Fintech & Trading", "ETF",
}

# Weekly quota policy
WEEKLY_CONTRACTS = 40
WEEKLY_TICKERS = 20
CONTRACTS_PER_TICKER = 5          # allows one full 5-lot spread on a ticker
SINGLE_LEG_SHARE = 0.40           # 40% single-leg : 60% spreads (unused single slots spill to spreads)
SHORT_CALL_CAP = 4                # undefined-risk cap inside the single-leg pool
SECTOR_CAP = 2                    # max tickers per sector among planned tickers
MAX_SPREAD_CONTRACTS = 5          # contracts per spread order (round-robin sized)
WEEKLY_COLLATERAL_CAP = 100_000.0 # total weekly collateral / BPR (USD)
WEEKLY_CREDIT_TARGET = 5000.0     # USD per week across planned contracts

# Sector health vetoes
SECTOR_WEAK_RSI = 40.0            # bull-side blocked when sector mean RSI(14) below this
SECTOR_STRONG_RSI = 62.0          # bear-side blocked when sector mean RSI(14) above this
SECTOR_STRONG_FLOW = 30.0         # |mean flow composite| beyond this = directional sector

FLOW_STRONG = 40.0
FLOW_LEAN = 15.0
FLOW_WEIGHTS = {"verdict": 0.35, "premium_today": 0.30, "premium_3d": 0.20, "darkpool": 0.15}
VERDICT_SIGN = {
    "bullish": 1.0,
    "bearish": -1.0,
    "protective": -0.5,
    "covered-call-selling": -0.3,
    "mixed": 0.0,
    "no-data": 0.0,
}
FLOW_STALE_DAYS = 5
VERDICT_STALE_DAYS = 4
DIRECTION_CLASS = {"bullish": 1, "bearish": -1, "protective": -1, "covered-call-selling": 0, "mixed": 0, "no-data": 0}

# Curated seed map (source='manual'). The UW profile endpoint needs Advanced tier and
# the Supabase watchlist has no sector column, so this seed is the primary source;
# overrides persist in `symbol_sector_map` once a live run writes them.
SECTOR_SEED: dict[str, str] = {
    # ETFs
    "SPY": "ETF", "QQQ": "ETF", "SOXX": "ETF", "SMH": "ETF",
    # Semiconductors
    "NVDA": "Semiconductor", "COHR": "Semiconductor", "INTC": "Semiconductor", "AMKR": "Semiconductor",
    "MU": "Semiconductor", "TSM": "Semiconductor", "FORM": "Semiconductor", "MRVL": "Semiconductor",
    "CRDO": "Semiconductor", "TXN": "Semiconductor", "TSEM": "Semiconductor", "TER": "Semiconductor",
    "ARM": "Semiconductor", "AMD": "Semiconductor", "SNDK": "Semiconductor", "ON": "Semiconductor",
    "AMBA": "Semiconductor", "AEHR": "Semiconductor", "ONTO": "Semiconductor", "QCOM": "Semiconductor",
    "ALGM": "Semiconductor", "ASML": "Semiconductor", "NXPI": "Semiconductor", "KLAC": "Semiconductor",
    "LRCX": "Semiconductor", "STM": "Semiconductor", "SWKS": "Semiconductor", "CEVA": "Semiconductor",
    "NVTS": "Semiconductor", "RMBS": "Semiconductor", "AVGO": "Semiconductor", "AMAT": "Semiconductor",
    "MTSI": "Semiconductor", "CAMT": "Semiconductor", "SMTC": "Semiconductor", "IMOS": "Semiconductor",
    "BESIY": "Semiconductor", "QRVO": "Semiconductor", "HIMX": "Semiconductor", "WOLF": "Semiconductor",
    "MCHP": "Semiconductor", "ADI": "Semiconductor", "MPWR": "Semiconductor", "ALAB": "Semiconductor",
    "AXTI": "Semiconductor", "AMBQ": "Semiconductor", "WDC": "Tech Hardware", "STX": "Tech Hardware",
    # Software & Cloud
    "ZS": "Software & Cloud", "MDB": "Software & Cloud", "FTNT": "Software & Cloud", "DOCN": "Software & Cloud",
    "PANW": "Software & Cloud", "CRWD": "Software & Cloud", "NET": "Software & Cloud", "DDOG": "Software & Cloud",
    "SNOW": "Software & Cloud", "CRM": "Software & Cloud", "DUOL": "Software & Cloud", "PLTR": "Software & Cloud",
    "AKAM": "Software & Cloud", "TWLO": "Software & Cloud", "CDNS": "Software & Cloud", "SNPS": "Software & Cloud",
    "BRZE": "Software & Cloud", "SHOP": "Software & Cloud", "NOW": "Software & Cloud", "PDFS": "Software & Cloud",
    "FROG": "Software & Cloud", "MSFT": "Software & Cloud",
    # Internet & Media
    "GOOGL": "Internet & Media", "AMZN": "Internet & Media", "META": "Internet & Media",
    # Tech Hardware & Networking
    "NOK": "Tech Hardware", "LITE": "Tech Hardware", "IONQ": "Tech Hardware", "TTMI": "Tech Hardware",
    "JBL": "Tech Hardware", "CIEN": "Networking Equipment", "ANET": "Networking Equipment", "VICR": "Tech Hardware",
    "RTX": "Aerospace & Defense", "PENG": "Tech Hardware", "SONY": "Tech Hardware", "AAOI": "Tech Hardware",
    "AAPL": "Tech Hardware", "HPE": "Tech Hardware", "DELL": "Tech Hardware", "OUST": "Tech Hardware",
    "FN": "Networking Equipment", "CSCO": "Networking Equipment", "UCTT": "Tech Hardware", "ICHR": "Tech Hardware",
    "FLNC": "Tech Hardware", "QUBT": "Tech Hardware", "POET": "Tech Hardware", "VIAV": "Tech Hardware",
    "KEYS": "Tech Hardware", "AVEX": "Tech Hardware",
    # Fintech & Trading
    "HOOD": "Fintech & Trading", "CRCL": "Fintech & Trading", "V": "Fintech & Trading",
    # Data Center & Crypto
    "CIFR": "Data Center & Crypto", "IREN": "Data Center & Crypto",
    "APLD": "Data Center & Crypto", "CLSK": "Data Center & Crypto", "NBIS": "Data Center & Crypto",
    "CRWV": "Data Center & Crypto", "VRT": "Data Center & Crypto",
    # Energy
    "COP": "Energy", "MPC": "Energy", "VLO": "Energy", "WMB": "Energy", "OKE": "Energy", "TRGP": "Energy",
    "HAL": "Energy", "CRGY": "Energy", "DINO": "Energy", "TALO": "Energy", "LNG": "Energy", "LEU": "Energy",
    "PSX": "Energy", "FANG": "Energy", "OXY": "Energy", "BE": "Energy", "XOM": "Energy",
    # Healthcare
    "ABBV": "Healthcare", "CNC": "Healthcare", "BIIB": "Healthcare", "DVA": "Healthcare", "PGNY": "Healthcare",
    "OSCR": "Healthcare", "GH": "Healthcare", "ITGR": "Healthcare", "LFST": "Healthcare", "TEM": "Healthcare",
    "UNH": "Healthcare", "ELV": "Healthcare", "LLY": "Healthcare", "HUM": "Healthcare", "CI": "Healthcare",
    "A": "Healthcare",
    # Consumer
    "PM": "Consumer", "KO": "Consumer", "MNST": "Consumer", "SBUX": "Consumer", "KDP": "Consumer",
    # Materials & Industrials
    "GLW": "Industrials", "PWR": "Industrials", "MOD": "Industrials", "NUE": "Materials", "STLD": "Materials",
    # Aerospace & Defense (RTX above)
    "RKLB": "Aerospace & Defense", "BA": "Aerospace & Defense",
    # --- watch2 / watch3 additions (2026-11-07 full-universe dry run) ---
    # Aerospace & Defense
    "BKSY": "Aerospace & Defense", "LUNR": "Aerospace & Defense", "KTOS": "Aerospace & Defense",
    "AVAV": "Aerospace & Defense", "PL": "Aerospace & Defense", "BWXT": "Aerospace & Defense",
    "HWM": "Aerospace & Defense", "CACI": "Aerospace & Defense", "GE": "Aerospace & Defense",
    # Financials & Banking
    "JPM": "Financials & Banking", "BAC": "Financials & Banking", "TROW": "Financials & Banking",
    "PFG": "Financials & Banking", "NDAQ": "Financials & Banking", "MET": "Financials & Banking",
    "BEN": "Financials & Banking", "IVZ": "Financials & Banking", "BLK": "Financials & Banking",
    "GS": "Financials & Banking", "EG": "Financials & Banking", "AIZ": "Financials & Banking",
    "ALL": "Financials & Banking", "MS": "Financials & Banking", "GL": "Financials & Banking",
    "APO": "Financials & Banking",
    # Fintech & Trading extras
    "COIN": "Fintech & Trading", "IBKR": "Fintech & Trading", "XYZ": "Fintech & Trading", "CPAY": "Fintech & Trading",
    # REITs
    "IRM": "REITs", "FRT": "REITs", "VTR": "REITs", "EQR": "REITs", "UDR": "REITs", "INVH": "REITs",
    "HST": "REITs", "DOC": "REITs", "ESS": "REITs", "AVB": "REITs", "PLD": "REITs", "AMT": "REITs", "WELL": "REITs",
    # Utilities & Power
    "GEV": "Utilities & Power", "EOSE": "Utilities & Power", "OKLO": "Utilities & Power",
    "NEE": "Utilities & Power", "FSLR": "Utilities & Power",
    # Transport & Logistics
    "DAL": "Transport & Logistics", "CSX": "Transport & Logistics", "NSC": "Transport & Logistics",
    "UNP": "Transport & Logistics", "EXPD": "Transport & Logistics",
    # Media & Entertainment
    "TTWO": "Media & Entertainment", "FOXA": "Media & Entertainment", "FOX": "Media & Entertainment",
    "SPOT": "Media & Entertainment", "LYV": "Media & Entertainment",
    # Telecom
    "ASTS": "Telecom", "SATS": "Telecom",
    # Industrials
    "CAT": "Industrials", "CMI": "Industrials", "AME": "Industrials", "ROK": "Industrials",
    "ETN": "Industrials", "SWK": "Industrials", "CARR": "Industrials", "PH": "Industrials",
    "FIX": "Industrials", "GWW": "Industrials", "URI": "Industrials", "EME": "Industrials",
    "WAB": "Industrials", "GNRC": "Industrials", "IEX": "Industrials", "JCI": "Industrials",
    # Materials
    "MP": "Materials", "MTRN": "Materials", "NEM": "Materials", "IFF": "Materials",
    "FCX": "Materials", "DD": "Materials", "DOW": "Materials",
    # Consumer
    "TSLA": "Consumer", "HAS": "Consumer", "ADM": "Consumer", "MO": "Consumer", "TSN": "Consumer",
    "MDLZ": "Consumer", "WMT": "Consumer", "COST": "Consumer", "F": "Consumer", "CASY": "Consumer",
    # Healthcare
    "CVS": "Healthcare", "SOLV": "Healthcare", "VTRS": "Healthcare", "WST": "Healthcare",
    # Tech
    "DIOD": "Semiconductor", "LWLG": "Tech Hardware", "NTAP": "Tech Hardware", "FFIV": "Software & Cloud",
    "VRSN": "Internet & Media", "EBAY": "Internet & Media",
    # Crypto
    "MARA": "Data Center & Crypto",
    # euro (non-US listings; filtered by exchange, mapped for completeness)
    "AIR": "Aerospace & Defense", "IFX": "Semiconductor", "AIXA": "Software & Cloud",
    # 2026-11-08 diversification additions (watch3): biotech/pharma + consumer staples
    "AMGN": "Biotech & Pharma", "GILD": "Biotech & Pharma", "REGN": "Biotech & Pharma",
    "VRTX": "Biotech & Pharma", "BMY": "Biotech & Pharma", "PFE": "Biotech & Pharma",
    "MRK": "Biotech & Pharma",
    "PG": "Consumer Staples", "TGT": "Consumer Staples", "PEP": "Consumer Staples",
    "CL": "Consumer Staples", "KMB": "Consumer Staples", "HSY": "Consumer Staples",
    "GIS": "Consumer Staples", "SYY": "Consumer Staples", "KHC": "Consumer Staples",
}


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
    valuation_tier: str = "not_meaningful"  # cheap|fair_low|fair_high|expensive|not_meaningful
    earnings_date: str | None = None
    earnings_report_time: str | None = None


@dataclass
class UWVolatilityContext:
    symbol: str
    iv7: float | None = None
    iv30: float | None = None
    iv60: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    term_ratio_7_30: float | None = None
    rsi_14: float | None = None


@dataclass
class SymbolSignalBundle:
    symbol: str
    watchlist_category: str = "core"
    spot_price: float | None = None
    quote_as_of: str | None = None
    quote_status: str = "live"

    # Flow verdict + fused flow score
    verdict: str = "no-data"
    verdict_reasons: list[str] = field(default_factory=list)
    flow_signal_date: str | None = None
    flow_alert_count: int | None = None
    verdict_date: str | None = None
    verdict_stale: bool = False
    alert_count_total: int | None = None
    alert_premium_tilt: float | None = None
    verdict_quant: str | None = None
    flow_history: list[dict[str, Any]] = field(default_factory=list)
    flow_composite: float | None = None
    flow_label: str = "neutral"
    flow_conflicts: list[str] = field(default_factory=list)
    flow_components: dict[str, Any] = field(default_factory=dict)

    # J Law structure (anchor supplement)
    jlaw_run_date: str | None = None
    jlaw_classification: str | None = None
    jlaw_score: int | None = None
    jlaw_stop_loss: float | None = None
    jlaw_target_price: float | None = None

    # Blended GEX snapshot (reference / wall fallback)
    gex_snapshot_ts: str | None = None
    gex_regime: str = "sub_resolution"
    gex_put_wall: float | None = None
    gex_call_wall: float | None = None
    gex_net_mm: float | None = None

    # Dark Pool
    dp_as_of: str | None = None
    dp_total_premium_mm: float | None = None
    dp_net_mm: float | None = None
    dp_top_level: float | None = None
    dp_top_level_mm: float | None = None
    dp_accumulating: bool | None = None

    # Sector & regime context
    sector: str = "Unknown"

    # UW contexts
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
    gamma: float | None
    iv: float | None
    open_interest: float


@dataclass
class BucketWalls:
    bucket: str
    put_wall: float | None = None
    call_wall: float | None = None
    put_wall_gex_mm: float | None = None
    call_wall_gex_mm: float | None = None
    source: str = "bucket_chain"  # bucket_chain|gex_blend|none


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
# Unusual Whales Fundamental & Volatility Evaluators (adapted from v1)
# ---------------------------------------------------------------------------


def compute_uw_valuation_context(
    symbol: str,
    spot_price: float | None,
    earnings_rows: list[dict[str, Any]],
    as_of_date: date | None = None,
) -> UWValuationContext:
    """TTM EPS / P/E / tier from `/api/stock/{ticker}/earnings`; next earnings date."""
    ref_date = as_of_date or date.today()
    ctx = UWValuationContext(symbol=symbol)

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

    if ctx.ttm_eps is None or ctx.ttm_eps <= 0:
        ctx.valuation_tier = "not_meaningful"
        return ctx

    if (ctx.pe_ratio is not None and ctx.pe_ratio <= 18.0) or (ctx.peg_ratio is not None and 0 < ctx.peg_ratio <= 1.2):
        ctx.valuation_tier = "cheap"
    elif (ctx.pe_ratio is not None and ctx.pe_ratio <= 28.0) or (ctx.peg_ratio is not None and 0 < ctx.peg_ratio <= 2.0):
        ctx.valuation_tier = "fair_low"
    elif ctx.pe_ratio is not None and ctx.pe_ratio <= 40.0:
        ctx.valuation_tier = "fair_high"
    else:
        ctx.valuation_tier = "expensive"
    return ctx


def compute_uw_volatility_context(
    symbol: str,
    interpolated_rows: list[dict[str, Any]],
    rsi_rows: list[dict[str, Any]] | None = None,
) -> UWVolatilityContext:
    """IV term structure from `/api/stock/{ticker}/interpolated-iv`, RSI(14) daily."""
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

    if vctx.iv7 and vctx.iv30 and vctx.iv30 > 0:
        vctx.term_ratio_7_30 = round(vctx.iv7 / vctx.iv30, 3)

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
    return vctx


def evaluate_strike_flow_prints(
    option_trades: list[dict[str, Any]],
    option_type: str,
    target_strike: float,
    tolerance_pct: float = 0.03,
) -> tuple[int, int]:
    """`(adverse_sweeps, confirming_whale_bids)` from `/api/option-trades` near the strike."""
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
# Combined Unusual-Flow Score (from v1, unchanged semantics)
# ---------------------------------------------------------------------------


def verdict_is_stale(verdict_date: Any, as_of_date: date) -> bool:
    d = parse_iso_date(verdict_date)
    return d is None or (as_of_date - d).days > VERDICT_STALE_DAYS


def _premium_tilt(row: dict[str, Any]) -> float:
    net = safe_float(row.get("net_directional_premium")) or 0.0
    conf = safe_float(row.get("directional_confidence")) or 0.0
    unusual_mm = (safe_float(row.get("unusual_premium")) or 0.0) / 1e6
    if net == 0.0 or conf <= 0.0 or unusual_mm <= 0.0:
        return 0.0
    size = max(0.3, min(1.0, math.log10(max(unusual_mm, 1.0)) / 2.0))
    return max(-1.0, min(1.0, math.copysign(conf * size, net)))


def flow_label_for(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score >= FLOW_STRONG:
        return "strong_bull"
    if score >= FLOW_LEAN:
        return "bull"
    if score <= -FLOW_STRONG:
        return "strong_bear"
    if score <= -FLOW_LEAN:
        return "bear"
    return "neutral"


def compute_flow_composite(bundle: SymbolSignalBundle, as_of_date: date) -> SymbolSignalBundle:
    comps: dict[str, float] = {}
    verdict = str(bundle.verdict or "no-data").strip().lower()
    if bundle.verdict_date is not None and verdict_is_stale(bundle.verdict_date, as_of_date):
        bundle.verdict_stale = True
    if verdict in VERDICT_SIGN and verdict != "no-data" and not bundle.verdict_stale:
        total = bundle.alert_count_total
        tilt = bundle.alert_premium_tilt
        if total is not None and tilt is not None:
            conf = max(0.3, min(1.0, total / 40.0))
            leg = 0.5 * VERDICT_SIGN[verdict] * conf + 0.5 * max(-1.0, min(1.0, tilt))
        else:
            ac = bundle.flow_alert_count
            conf = 0.6 if ac is None else max(0.3, min(1.0, ac / 10.0))
            leg = VERDICT_SIGN[verdict] * conf
        comps["verdict"] = max(-1.0, min(1.0, leg))

    hist = sorted(
        [h for h in (bundle.flow_history or []) if h.get("signal_date")],
        key=lambda h: str(h["signal_date"]),
        reverse=True,
    )
    latest_date = parse_iso_date(hist[0]["signal_date"]) if hist else None
    if latest_date and (as_of_date - latest_date).days <= FLOW_STALE_DAYS:
        tilts = [_premium_tilt(h) for h in hist[:3]]
        comps["premium_today"] = tilts[0]
        comps["premium_3d"] = sum(tilts) / 3.0

    dp_tot = bundle.dp_total_premium_mm or 0.0
    if dp_tot > 0 and bundle.dp_net_mm is not None:
        comps["darkpool"] = max(-1.0, min(1.0, 1.5 * bundle.dp_net_mm / dp_tot))

    conflicts: list[str] = []
    today = comps.get("premium_today")
    if verdict in {"bullish", "bearish"} and today is not None and abs(today) >= 0.4:
        if (VERDICT_SIGN[verdict] > 0) != (today > 0):
            conflicts.append("verdict_vs_premium")
    q = DIRECTION_CLASS.get(str(bundle.verdict_quant or "").lower(), 0)
    a = DIRECTION_CLASS.get(verdict, 0)
    if not bundle.verdict_stale and a != 0 and q != 0 and a != q:
        conflicts.append("verdict_vs_alert_premium")
    options_side = [comps[k] for k in ("verdict", "premium_today") if k in comps]
    dp = comps.get("darkpool")
    if dp is not None and abs(dp) >= 0.5 and options_side:
        opt = sum(options_side) / len(options_side)
        if abs(opt) >= 0.2 and (opt > 0) != (dp > 0):
            conflicts.append("darkpool_vs_options")

    opt_keys = [k for k in ("verdict", "premium_today", "premium_3d") if k in comps]
    if opt_keys:
        w_opt = sum(FLOW_WEIGHTS[k] for k in opt_keys)
        opt_score = sum(FLOW_WEIGHTS[k] * comps[k] for k in opt_keys) / w_opt
        if dp is not None:
            score = round(100.0 * ((1.0 - FLOW_WEIGHTS["darkpool"]) * opt_score + FLOW_WEIGHTS["darkpool"] * dp), 1)
        else:
            score = round(100.0 * opt_score, 1)
        label = flow_label_for(score)
    elif dp is not None:
        score = round(100.0 * FLOW_WEIGHTS["darkpool"] * dp, 1)
        label = "neutral"
    else:
        score, label = None, "neutral"
    bundle.flow_composite = score
    bundle.flow_label = label
    bundle.flow_conflicts = conflicts
    bundle.flow_components = {
        **{k: round(v, 3) for k, v in comps.items()},
        "missing": [k for k in FLOW_WEIGHTS if k not in comps],
        "flow_signal_date": latest_date.isoformat() if latest_date else None,
        "verdict_date": bundle.verdict_date,
        "verdict_stale": bundle.verdict_stale,
        "verdict_quant": bundle.verdict_quant,
    }
    return bundle


def flow_alignment(strategy: str, composite: float | None) -> float:
    c = (composite or 0.0) / 100.0
    if strategy in BULL_SIDE_STRATEGIES:
        return c
    if strategy in BEAR_SIDE_STRATEGIES:
        return -c
    return 0.0


# ---------------------------------------------------------------------------
# Term-Structure Bucket Walls (the v2 core: DTE-specific call/put walls)
# ---------------------------------------------------------------------------


def compute_bucket_walls(
    contracts: list[OptionContractQuote],
    spot: float | None,
    bucket: str,
    gex_blend_put_wall: float | None = None,
    gex_blend_call_wall: float | None = None,
) -> BucketWalls:
    """
    DTE-specific walls for one bucket, mirroring the `uw-gex` methodology but
    restricted to the bucket's expiry window:

      1. Keep contracts with DTE inside the bucket and OI > 0.
      2. Per side, keep only the nearest-DTE expiry per strike (a strike listed in
         both the 6-dte and 13-dte Friday contributes only its 6-dte row to LT8).
      3. Per-strike magnitude = gamma x OI x 100 x spot x 0.01 / 1e6  ($MM per 1% move).
      4. call_wall = max-magnitude call strike at/above spot; put_wall = max-magnitude
         put strike at/below spot (walls sit on the correct side of spot by definition).

    Fallback: when the chain cannot produce a wall (no gamma / no OI / one-sided),
    fall back to the Supabase `gex_snapshot` blended wall and mark wall_source =
    'gex_blend'. Walls are mandatory for strike anchoring; when neither source
    yields a wall the caller must skip the strategy.
    """
    walls = BucketWalls(bucket=bucket)
    lo, hi = DTE_BUCKETS[bucket]
    if not spot or spot <= 0:
        walls.source = "gex_blend" if (gex_blend_put_wall or gex_blend_call_wall) else "none"
        walls.put_wall = gex_blend_put_wall
        walls.call_wall = gex_blend_call_wall
        return walls

    rows = [
        c for c in contracts
        if lo <= c.dte <= hi and c.open_interest > 0 and c.gamma is not None and c.gamma > 0
    ]
    for side, wall_attr, mm_attr, strike_filter in (
        ("put", "put_wall", "put_wall_gex_mm", lambda k: k <= spot),
        ("call", "call_wall", "call_wall_gex_mm", lambda k: k >= spot),
    ):
        # nearest-DTE expiry per strike, within the bucket
        nearest: dict[float, OptionContractQuote] = {}
        for c in rows:
            if c.option_type != side:
                continue
            prev = nearest.get(c.strike)
            if prev is None or c.dte < prev.dte:
                nearest[c.strike] = c
        best_strike: float | None = None
        best_mag = -1.0
        for strike, q in nearest.items():
            if not strike_filter(strike):
                continue
            mag = (q.gamma or 0.0) * q.open_interest * 100.0 * spot * 0.01 / 1e6
            if mag > best_mag:
                best_mag = mag
                best_strike = strike
        if best_strike is not None:
            setattr(walls, wall_attr, round(best_strike, 4))
            setattr(walls, mm_attr, round(best_mag, 3))

    if walls.put_wall is None and walls.call_wall is None:
        walls.source = "gex_blend" if (gex_blend_put_wall or gex_blend_call_wall) else "none"
        walls.put_wall = gex_blend_put_wall
        walls.call_wall = gex_blend_call_wall
    elif walls.put_wall is None or walls.call_wall is None:
        # one-sided chain wall: fill the missing side from the blend
        walls.source = "bucket_chain+blend"
        if walls.put_wall is None:
            walls.put_wall = gex_blend_put_wall
        if walls.call_wall is None:
            walls.call_wall = gex_blend_call_wall
    return walls


# ---------------------------------------------------------------------------
# Sector & Market Regime Context
# ---------------------------------------------------------------------------


def classify_market_regime(regime_text: str | None) -> str:
    """Map the `trade-jlaw-v2.regime` run note to RISK_ON | RISK_OFF | CHOP | UNKNOWN."""
    t = str(regime_text or "").strip().lower()
    if not t:
        return "UNKNOWN"
    if "risk-off" in t or "risk off" in t or "distribution" in t or "defensive" in t:
        return "RISK_OFF"
    if "risk-on" in t or "risk on" in t:
        return "RISK_ON"
    if "chop" in t or "range" in t or "sideways" in t or "volatile" in t or "neutral" in t:
        return "CHOP"
    return "UNKNOWN"


def aggregate_sector_health(bundles: list[SymbolSignalBundle]) -> dict[str, dict[str, float | None]]:
    """Sector -> {"mean_rsi", "mean_flow", "members"} over the scanned universe."""
    by_sector: dict[str, list[SymbolSignalBundle]] = {}
    for b in bundles:
        sec = b.sector or "Unknown"
        by_sector.setdefault(sec, []).append(b)

    out: dict[str, dict[str, float | None]] = {}
    for sec, members in by_sector.items():
        rsis = [m.volatility.rsi_14 for m in members if m.volatility and m.volatility.rsi_14 is not None]
        flows = [m.flow_composite for m in members if m.flow_composite is not None]
        out[sec] = {
            "mean_rsi": round(statistics.mean(rsis), 2) if rsis else None,
            "mean_flow": round(statistics.mean(flows), 1) if flows else None,
            "members": len(members),
        }
    return out


def sector_blocks_side(sector: str, side: str, sector_health: dict[str, dict[str, float | None]]) -> str | None:
    """
    Weak sectors (mean RSI < 40 or strongly bear flow) block bull-side structures;
    strong sectors (mean RSI > 62 or strongly bull flow) block bear-side structures.
    'Unknown' sector (or sectors without data) never blocks — reported only.
    """
    h = sector_health.get(sector)
    if not h or sector == "Unknown":
        return None
    rsi = h.get("mean_rsi")
    flow = h.get("mean_flow")
    if side == "bull":
        if rsi is not None and rsi < SECTOR_WEAK_RSI:
            return f"SECTOR_WEAK_RSI_{rsi:.0f}"
        if flow is not None and flow <= -SECTOR_STRONG_FLOW:
            return "SECTOR_STRONG_BEAR_FLOW"
    elif side == "bear":
        if rsi is not None and rsi > SECTOR_STRONG_RSI:
            return f"SECTOR_STRONG_RSI_{rsi:.0f}"
        if flow is not None and flow >= SECTOR_STRONG_FLOW:
            return "SECTOR_STRONG_BULL_FLOW"
    return None


def regime_tilt(strategy: str, regime: str) -> float:
    """Planner-only score adjustment (stored in plan_score_adjusted)."""
    if regime == "RISK_ON":
        return 1.5 if strategy in BULL_SIDE_STRATEGIES else -1.0
    if regime == "RISK_OFF":
        if strategy in BEAR_SIDE_STRATEGIES:
            return 1.5
        return -1.5 if strategy in SINGLE_LEG_STRATEGIES else -0.5
    if regime == "CHOP":
        return 1.0 if strategy in SPREAD_STRATEGIES else -1.0
    return 0.0


# Sector-performance routing: bull sectors -> put side, bear sectors -> call side.
SECTOR_TREND_RSI_BEAR = 45.0
SECTOR_TREND_RSI_BULL = 55.0
SECTOR_TREND_FLOW = 15.0


def sector_trend_for(c: dict[str, Any]) -> str:
    """'bull' | 'bear' | 'neutral' from the candidate's sector health (mean RSI / mean flow)."""
    rsi = c.get("sector_health_rsi")
    flow = c.get("sector_health_flow")
    bear = (rsi is not None and rsi < SECTOR_TREND_RSI_BEAR) or (flow is not None and flow <= -SECTOR_TREND_FLOW)
    bull = (rsi is not None and rsi > SECTOR_TREND_RSI_BULL) or (flow is not None and flow >= SECTOR_TREND_FLOW)
    if bear and not bull:
        return "bear"
    if bull and not bear:
        return "bull"
    return "neutral"


def sector_aligned(c: dict[str, Any]) -> bool:
    """True when the strategy side matches the sector trend (bear side in a bear sector, etc.).

    Aligned picks are exempt from the per-sector ticker cap: a bearish sector with many
    qualified SHORT_CALL / BEAR_CALL_SPREAD names should not be crowded out — short calls
    and BCS are exactly the right structures for a bearish trend.
    """
    side = "bull" if c["strategy"] in BULL_SIDE_STRATEGIES else "bear"
    return sector_trend_for(c) == side


# ---------------------------------------------------------------------------
# Earnings Clearance
# ---------------------------------------------------------------------------


def is_earnings_clear(
    report_date: date,
    expiry_date: date,
    earnings_date_str: str | None,
    earnings_report_time: str | None = None,
) -> bool:
    edate = parse_iso_date(earnings_date_str)
    if not edate:
        return True
    if edate < expiry_date:
        return False
    if edate == expiry_date:
        # Only a postmarket print on expiry day still settles cleanly.
        return str(earnings_report_time or "").lower() == "postmarket"
    return True


# ---------------------------------------------------------------------------
# Confidence Scoring (0-100, 7 pillars; pillar 3 anchors on bucket walls)
# ---------------------------------------------------------------------------


def score_opportunity(
    strategy: str,
    bundle: SymbolSignalBundle,
    short_leg: OptionContractQuote,
    walls: BucketWalls,
    sector_health: dict[str, dict[str, float | None]],
    return_on_risk: float | None,
    annualized_pct: float | None,
    safety_floor: float | None,
    safety_ceiling: float | None,
    adverse_sweeps: int,
    confirming_bids: int,
) -> tuple[float, str, dict[str, float]]:
    val = bundle.valuation or UWValuationContext(symbol=bundle.symbol)
    vol = bundle.volatility or UWVolatilityContext(symbol=bundle.symbol)

    # 1. Structural Anchor & Stop Buffer (0-20): bucket-wall compliance + J Law quality
    jlaw_pts = 8.0
    cls = str(bundle.jlaw_classification or "").strip().lower()
    if cls == "valid":
        jlaw_pts = 14.0
    elif cls == "watch":
        jlaw_pts = 11.0
    elif cls == "invalid":
        jlaw_pts = 4.0
    if bundle.jlaw_score is not None:
        jlaw_pts += min(2.0, max(0.0, (bundle.jlaw_score - 8) * 0.4))
    # bucket-wall compliance bonus (the v2 loss-minimisation core)
    if short_leg.option_type == "put" and walls.put_wall and short_leg.strike <= walls.put_wall:
        jlaw_pts += 4.0 if walls.source.startswith("bucket_chain") else 2.0
    if short_leg.option_type == "call" and walls.call_wall and short_leg.strike >= walls.call_wall:
        jlaw_pts += 4.0 if walls.source.startswith("bucket_chain") else 2.0
    jlaw_pts = round(min(20.0, jlaw_pts), 2)

    # 2. Combined Unusual Flow (0-20)
    align = flow_alignment(strategy, bundle.flow_composite)
    flow_pts = 10.0 + 8.0 * align
    if bundle.flow_conflicts:
        flow_pts -= 3.0
    if confirming_bids > 0:
        flow_pts += 2.0
    if adverse_sweeps > 0:
        flow_pts -= 8.0
    flow_pts = round(max(0.0, min(20.0, flow_pts)), 2)

    # 3. Bucket Wall & Corridor Quality (0-15)
    gex_pts = 5.0
    if bundle.gex_regime == "positive":
        gex_pts = 10.0
    elif bundle.gex_regime == "negative":
        gex_pts = 7.0
    if walls.source.startswith("bucket_chain"):
        gex_pts += 3.0
    if walls.bucket in DTE_BUCKETS:
        lo, hi = DTE_BUCKETS[walls.bucket]
        if lo <= short_leg.dte <= hi:
            gex_pts += 2.0
    gex_pts = round(min(15.0, gex_pts), 2)

    # 4. UW Fundamental Quality (0-15)
    fund_pts = 7.0
    if val.valuation_tier == "cheap":
        fund_pts = 12.0
    elif val.valuation_tier == "fair_low":
        fund_pts = 10.5
    elif val.valuation_tier == "fair_high":
        fund_pts = 9.0
    elif val.valuation_tier == "expensive":
        fund_pts = 6.0
    if val.latest_surprise_pct is not None and val.latest_surprise_pct > 2.0:
        fund_pts += 1.5
    if strategy in BULL_SIDE_STRATEGIES and val.valuation_tier == "not_meaningful":
        fund_pts = max(0.0, fund_pts - 2.0)
    fund_pts = round(min(15.0, fund_pts), 2)

    # 5. Volatility & Term-Structure Fit (0-10)
    ivr = vol.iv_rank if vol.iv_rank is not None else 35.0
    vol_pts = 5.0
    if ivr >= 40.0:
        vol_pts = 9.0
    elif ivr >= 25.0:
        vol_pts = 7.5
    if vol.term_ratio_7_30 is not None and vol.term_ratio_7_30 > 1.20:
        vol_pts = max(0.0, vol_pts - 1.5)  # front-week event shock
    vol_pts = round(min(10.0, vol_pts), 2)

    # 6. Execution & Liquidity (0-10)
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

    # 7. Economics, Sector & Timing (0-10)
    econ_pts = 4.0
    if return_on_risk is not None and return_on_risk >= SPREAD_ROR_BAND[0] + 0.05:
        econ_pts += 2.0
    if annualized_pct is not None and annualized_pct >= SINGLE_LEG_ANNUALIZED.get(walls.bucket, 25.0):
        econ_pts += 1.0
    rsi = vol.rsi_14
    if rsi is not None:
        if strategy in BULL_SIDE_STRATEGIES and 35.0 <= rsi <= 58.0:
            econ_pts += 2.0
        elif strategy in BEAR_SIDE_STRATEGIES and rsi >= 58.0:
            econ_pts += 2.0
    sh = sector_health.get(bundle.sector)
    if sh and sh.get("mean_rsi") is not None:
        if strategy in BULL_SIDE_STRATEGIES and sh["mean_rsi"] >= 50.0:
            econ_pts += 1.0
        elif strategy in BEAR_SIDE_STRATEGIES and sh["mean_rsi"] <= 50.0:
            econ_pts += 1.0
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
        "structural_anchor": jlaw_pts,
        "flow": flow_pts,
        "bucket_wall": gex_pts,
        "uw_fundamental": fund_pts,
        "volatility_fit": vol_pts,
        "liquidity": liq_pts,
        "economics_sector": econ_pts,
    }
    return total, band, components


# ---------------------------------------------------------------------------
# Opportunity Row Builder
# ---------------------------------------------------------------------------


def _make_opportunity_row(
    report_date: date,
    run_id: str,
    strategy: str,
    dte_bucket: str,
    bundle: SymbolSignalBundle,
    walls: BucketWalls,
    short_leg: OptionContractQuote,
    leg2: OptionContractQuote | None,
    entry_credit: float,
    collateral: float,
    max_loss: float | None,
    return_on_risk: float | None,
    annualized_pct: float | None,
    break_even_low: float | None,
    break_even_high: float | None,
    safety_floor: float | None,
    safety_ceiling: float | None,
    adverse_sweeps: int,
    confirming_bids: int,
    sector_health: dict[str, dict[str, float | None]],
    market_regime: str,
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
        walls=walls,
        sector_health=sector_health,
        return_on_risk=return_on_risk,
        annualized_pct=annualized_pct,
        safety_floor=safety_floor,
        safety_ceiling=safety_ceiling,
        adverse_sweeps=adverse_sweeps,
        confirming_bids=confirming_bids,
    )

    spot = bundle.spot_price or 0.0
    cautions: list[str] = []
    if walls.source == "gex_blend":
        cautions.append("WALL_GEX_BLEND_FALLBACK")
    if strategy == "SHORT_CALL":
        cautions.append("UNDEFINED_RISK")
    if bundle.gex_regime == "sub_resolution":
        cautions.append("GEX_SUB_RESOLUTION")
    if val.valuation_tier == "expensive" and strategy in BULL_SIDE_STRATEGIES:
        cautions.append("VALUATION_EXPENSIVE")
    if vol.term_ratio_7_30 and vol.term_ratio_7_30 > 1.15:
        cautions.append("FRONT_IV_ELEVATED")
    for c in bundle.flow_conflicts or []:
        cautions.append(f"FLOW_CONFLICT_{c.upper()}")
    if bundle.verdict_stale:
        cautions.append("VERDICT_STALE")

    put_wall_dist = (
        round((walls.put_wall - short_leg.strike) / spot * 100.0, 2)
        if spot and walls.put_wall and short_leg.option_type == "put" else None
    )
    call_wall_dist = (
        round((short_leg.strike - walls.call_wall) / spot * 100.0, 2)
        if spot and walls.call_wall and short_leg.option_type == "call" else None
    )

    sh = sector_health.get(bundle.sector) or {}
    row = {
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
        "leg2_role": "LONG_PROTECTION" if leg2 else None,
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
        "spread_width": abs(leg2.strike - short_leg.strike) if leg2 else None,
        "net_entry_price": round(entry_credit, 4),
        "entry_credit": round(entry_credit, 4),
        "credit_per_contract_usd": round(entry_credit * 100.0, 2),
        "entry_debit": None,
        "collateral": round(collateral, 4),
        "max_loss": round(max_loss, 4) if max_loss is not None else None,
        "return_on_risk": round(return_on_risk, 4) if return_on_risk is not None else None,
        "annualized_pct": round(annualized_pct, 2) if annualized_pct is not None else None,
        "break_even_low": round(break_even_low, 4) if break_even_low is not None else None,
        "break_even_high": round(break_even_high, 4) if break_even_high is not None else None,
        "delta_band_ok": True,
        "credit_gate_ok": True,
        "bucket_put_wall": walls.put_wall,
        "bucket_call_wall": walls.call_wall,
        "put_wall_gex_mm": walls.put_wall_gex_mm,
        "call_wall_gex_mm": walls.call_wall_gex_mm,
        "wall_source": walls.source,
        "short_to_put_wall_pct": put_wall_dist,
        "short_to_call_wall_pct": call_wall_dist,
        "safety_floor_price": safety_floor,
        "safety_ceiling_price": safety_ceiling,
        "sector": bundle.sector,
        "sector_health_rsi": sh.get("mean_rsi"),
        "sector_health_flow": sh.get("mean_flow"),
        "market_regime": market_regime,
        "uw_ttm_eps": val.ttm_eps,
        "uw_pe_ratio": val.pe_ratio,
        "uw_eps_growth_yoy_pct": val.eps_growth_yoy_pct,
        "uw_valuation_tier": val.valuation_tier,
        "earnings_date": val.earnings_date,
        "earnings_report_time": val.earnings_report_time,
        "earnings_clear": True,
        "uw_iv7": vol.iv7,
        "uw_iv30": vol.iv30,
        "uw_iv60": vol.iv60,
        "uw_iv_rank": vol.iv_rank,
        "uw_iv_percentile": vol.iv_percentile,
        "uw_term_ratio_7_30": vol.term_ratio_7_30,
        "uw_rsi_14": vol.rsi_14,
        "strike_adverse_sweeps": adverse_sweeps,
        "strike_whale_bids": confirming_bids,
        "verdict": bundle.verdict,
        "verdict_reasons": bundle.verdict_reasons,
        "flow_signal_date": bundle.flow_signal_date,
        "flow_alert_count": bundle.flow_alert_count,
        "flow_composite": bundle.flow_composite,
        "flow_label": bundle.flow_label,
        "flow_conflicts": bundle.flow_conflicts,
        "flow_components": bundle.flow_components,
        "jlaw_run_date": bundle.jlaw_run_date,
        "jlaw_classification": bundle.jlaw_classification,
        "jlaw_score": bundle.jlaw_score,
        "jlaw_stop_loss": bundle.jlaw_stop_loss,
        "jlaw_target_price": bundle.jlaw_target_price,
        "gex_snapshot_ts": bundle.gex_snapshot_ts,
        "gex_regime": bundle.gex_regime,
        "gex_put_wall": bundle.gex_put_wall,
        "gex_call_wall": bundle.gex_call_wall,
        "dp_as_of": bundle.dp_as_of,
        "dp_top_level": bundle.dp_top_level,
        "dp_top_level_mm": bundle.dp_top_level_mm,
        "dp_accumulating": bundle.dp_accumulating,
        "confidence_score": conf_score,
        "score_band": score_band,
        "score_components": score_components,
        "risk_flag": "UNDEFINED_RISK" if strategy == "SHORT_CALL" else None,
        "caution": "|".join(cautions) if cautions else None,
        "notes": notes,
        # lifecycle fields (kept in the row for the ledger; null until tracked)
        "profit50_hit_date": None,
        "profit50_close_mark": None,
        "midcycle_mark_date": None,
        "midcycle_close_mark": None,
        "midcycle_pnl": None,
        "defense_trigger_date": None,
        "defense_reason": None,
        "exit_date": None,
        "exit_close_mark": None,
        "exit_pnl": None,
        "outcome_notes": None,
        # weekly plan fields (filled by plan_weekly_quota)
        "plan_week": None,
        "planned_contracts": 0,
        "plan_rank": None,
        "plan_score_adjusted": None,
    }
    return row


# ---------------------------------------------------------------------------
# Stateless Strategy Candidate Generation (4 strategies x 2 DTE buckets)
# ---------------------------------------------------------------------------


def generate_opportunities_for_symbol(
    report_date: date,
    run_id: str,
    bundle: SymbolSignalBundle,
    contracts: list[OptionContractQuote],
    walls_by_bucket: dict[str, BucketWalls],
    option_trades: list[dict[str, Any]] | None = None,
    sector_health: dict[str, dict[str, float | None]] | None = None,
    market_regime: str = "UNKNOWN",
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Returns (candidate rows, veto reasons). One best row per (strategy, dte_bucket).
    All gates are hard; anything failing is silently absent from candidates but
    counted in veto reasons for the report.
    """
    sector_health = sector_health or {}
    vetoes: list[str] = []
    if not is_us_option_symbol(bundle.symbol):
        return [], ["NON_US_SYMBOL"]
    spot = bundle.spot_price
    if not spot or spot <= 0 or not contracts:
        return [], ["NO_SPOT_OR_CHAIN"]

    val = bundle.valuation or UWValuationContext(symbol=bundle.symbol)
    vol = bundle.volatility or UWVolatilityContext(symbol=bundle.symbol)
    trades = option_trades or []
    sector = bundle.sector or "Unknown"
    # width rule: tech trades $5-wide verticals, other sectors $10-wide (fallback the other width)
    width_pref = SPREAD_WIDTH_PREFERENCE if sector in TECH_SECTORS else SPREAD_WIDTH_PREFERENCE_NONTECH

    if bundle.flow_composite is None and not bundle.flow_components:
        compute_flow_composite(bundle, report_date)
    flow_c = bundle.flow_composite if bundle.flow_composite is not None else 0.0
    verdict = str(bundle.verdict or "no-data").strip().lower()
    ivr = vol.iv_rank if vol.iv_rank is not None else 35.0
    rsi = vol.rsi_14 if vol.rsi_14 is not None else 50.0
    results: list[dict[str, Any]] = []

    puts = [c for c in contracts if c.option_type == "put" and c.bid > 0 and c.ask >= c.bid and c.dte >= 1]
    calls = [c for c in contracts if c.option_type == "call" and c.bid > 0 and c.ask >= c.bid and c.dte >= 1]

    # Side-level regime gates
    bull_veto = None
    if bundle.flow_label == "strong_bear":
        bull_veto = "STRONG_BEAR_FLOW"
    elif (sv := sector_blocks_side(sector, "bull", sector_health)):
        bull_veto = sv
    bear_veto = None
    if bundle.flow_label == "strong_bull":
        bear_veto = "STRONG_BULL_FLOW"
    elif (sv := sector_blocks_side(sector, "bear", sector_health)):
        bear_veto = sv

    for bucket, (lo, hi) in DTE_BUCKETS.items():
        walls = walls_by_bucket.get(bucket)
        if walls is None or (walls.put_wall is None and walls.call_wall is None):
            vetoes.append(f"{bucket}:NO_WALL")
            continue

        bucket_puts = [p for p in puts if lo <= p.dte <= hi]
        bucket_calls = [c for c in calls if lo <= c.dte <= hi]

        # ---------------- SHORT_PUT ----------------
        if bull_veto:
            vetoes.append(f"{bucket}:SHORT_PUT:{bull_veto}")
        elif spot > SINGLE_LEG_MAX_SPOT:
            vetoes.append(f"{bucket}:SHORT_PUT:SPOT_ABOVE_{SINGLE_LEG_MAX_SPOT:.0f}")
        elif not (ivr >= 20.0):
            vetoes.append(f"{bucket}:SHORT_PUT:IVR_LOW")
        elif bundle.dp_accumulating is False:
            vetoes.append(f"{bucket}:SHORT_PUT:DP_DISTRIBUTING")
        else:
            put_anchor_candidates: list[float] = [walls.put_wall] if walls.put_wall else []
            if bundle.jlaw_stop_loss and 0 < bundle.jlaw_stop_loss < spot:
                put_anchor_candidates.append(bundle.jlaw_stop_loss)
            if not put_anchor_candidates:
                vetoes.append(f"{bucket}:SHORT_PUT:NO_PUT_ANCHOR")
            else:
                put_anchor = min(put_anchor_candidates)
                ann_min = SINGLE_LEG_ANNUALIZED[bucket]
                best: dict[str, Any] | None = None
                for p in bucket_puts:
                    if p.delta is None or not (SINGLE_DELTA_BAND[0] <= abs(p.delta) <= SINGLE_DELTA_BAND[1]):
                        continue
                    if p.strike > put_anchor:
                        continue
                    if p.bid < MIN_SINGLE_CREDIT:
                        continue
                    if not is_earnings_clear(report_date, date.fromisoformat(p.expiry), val.earnings_date, val.earnings_report_time):
                        continue
                    ann_pct = (p.bid / p.strike) * (365.0 / p.dte) * 100.0
                    if ann_pct < ann_min:
                        continue
                    adv, bids = evaluate_strike_flow_prints(trades, "put", p.strike)
                    if adv > 0:
                        continue
                    ror = p.bid / max(0.01, p.strike - p.bid)
                    row = _make_opportunity_row(
                        report_date, run_id, "SHORT_PUT", bucket, bundle, walls,
                        short_leg=p, leg2=None,
                        entry_credit=p.bid,
                        collateral=p.strike,
                        max_loss=p.strike - p.bid,
                        return_on_risk=ror,
                        annualized_pct=ann_pct,
                        break_even_low=p.strike - p.bid,
                        break_even_high=None,
                        safety_floor=put_anchor,
                        safety_ceiling=None,
                        adverse_sweeps=adv, confirming_bids=bids,
                        sector_health=sector_health, market_regime=market_regime,
                        notes=f"CSP anchored <= put wall {walls.put_wall} ({walls.source}); ann {ann_pct:.0f}%",
                    )
                    if best is None or row["confidence_score"] > best["confidence_score"]:
                        best = row
                if best:
                    results.append(best)
                else:
                    vetoes.append(f"{bucket}:SHORT_PUT:NO_QUALIFYING_STRIKE")

        # ---------------- BULL_PUT_SPREAD ----------------
        if bull_veto:
            vetoes.append(f"{bucket}:BULL_PUT_SPREAD:{bull_veto}")
        elif not (verdict in {"bullish", "mixed"}):
            vetoes.append(f"{bucket}:BULL_PUT_SPREAD:VERDICT_{verdict.upper() or 'NODATA'}")
        elif not (ivr >= 25.0):
            vetoes.append(f"{bucket}:BULL_PUT_SPREAD:IVR_LOW")
        else:
            put_anchor_candidates = [walls.put_wall] if walls.put_wall else []
            if bundle.jlaw_stop_loss and 0 < bundle.jlaw_stop_loss < spot:
                put_anchor_candidates.append(bundle.jlaw_stop_loss)
            if not put_anchor_candidates:
                vetoes.append(f"{bucket}:BULL_PUT_SPREAD:NO_PUT_ANCHOR")
            else:
                put_anchor = min(put_anchor_candidates)
                best = None
                by_exp: dict[str, list[OptionContractQuote]] = {}
                for p in bucket_puts:
                    by_exp.setdefault(p.expiry, []).append(p)
                # tech: prefer $5 verticals; other sectors: $10-wide; fallback to the other width
                for width in width_pref:
                    for exp, exp_puts in by_exp.items():
                        if not is_earnings_clear(report_date, date.fromisoformat(exp), val.earnings_date, val.earnings_report_time):
                            continue
                        by_strike = {round(p.strike, 2): p for p in exp_puts}
                        for sp in exp_puts:
                            if sp.delta is None or not (SPREAD_DELTA_BAND[0] <= abs(sp.delta) <= SPREAD_DELTA_BAND[1]):
                                continue
                            if sp.strike > put_anchor:
                                continue
                            lp = by_strike.get(round(sp.strike - width, 2))
                            if not lp or lp.ask <= 0:
                                continue
                            credit = round(sp.bid - lp.ask, 2)   # NBBO is in pennies; round BEFORE the strict gate
                            if credit <= SPREAD_MIN_CREDIT:      # strict NBBO credit > $0.70
                                continue
                            max_loss = width - credit
                            if max_loss <= 0:
                                continue
                            ror = credit / max_loss
                            if not (ROR_FLOOR_BY_WIDTH.get(width, SPREAD_ROR_BAND[0]) <= ror <= SPREAD_ROR_BAND[1]):
                                continue
                            adv, bids = evaluate_strike_flow_prints(trades, "put", sp.strike)
                            if adv > 0:
                                continue
                            ann_pct = ror * (365.0 / sp.dte) * 100.0
                            row = _make_opportunity_row(
                                report_date, run_id, "BULL_PUT_SPREAD", bucket, bundle, walls,
                                short_leg=sp, leg2=lp,
                                entry_credit=credit,
                                collateral=width,
                                max_loss=max_loss,
                                return_on_risk=ror,
                                annualized_pct=ann_pct,
                                break_even_low=sp.strike - credit,
                                break_even_high=None,
                                safety_floor=put_anchor,
                                safety_ceiling=None,
                                adverse_sweeps=adv, confirming_bids=bids,
                                sector_health=sector_health, market_regime=market_regime,
                                notes=f"BPS {width:.0f}-wide credit ${credit:.2f} > $0.70; short <= put wall {walls.put_wall}",
                            )
                            if best is None or row["confidence_score"] > best["confidence_score"]:
                                best = row
                    if best is not None:
                        break
                if best:
                    results.append(best)
                else:
                    vetoes.append(f"{bucket}:BULL_PUT_SPREAD:NO_QUALIFYING_SPREAD")

        # ---------------- SHORT_CALL (undefined-risk reference) ----------------
        call_ceiling = None
        if walls.call_wall:
            call_ceiling = max(walls.call_wall, round(spot * 1.03, 4))
        if bear_veto:
            vetoes.append(f"{bucket}:SHORT_CALL:{bear_veto}")
        elif spot > SINGLE_LEG_MAX_SPOT:
            vetoes.append(f"{bucket}:SHORT_CALL:SPOT_ABOVE_{SINGLE_LEG_MAX_SPOT:.0f}")
        elif not call_ceiling:
            vetoes.append(f"{bucket}:SHORT_CALL:NO_CALL_WALL")
        elif not (ivr >= 25.0):
            vetoes.append(f"{bucket}:SHORT_CALL:IVR_LOW")
        elif not (rsi >= 60.0 or verdict == "bearish" or bundle.flow_label in {"bear", "strong_bear"}):
            vetoes.append(f"{bucket}:SHORT_CALL:NOT_OVERBOUGHT_OR_BEARISH")
        else:
            ann_min = SINGLE_LEG_ANNUALIZED[bucket]
            best = None
            for c in bucket_calls:
                if c.delta is None or not (SINGLE_DELTA_BAND[0] <= c.delta <= SINGLE_DELTA_BAND[1]):
                    continue
                if c.strike < call_ceiling:
                    continue
                if c.bid < MIN_SINGLE_CREDIT:
                    continue
                if not is_earnings_clear(report_date, date.fromisoformat(c.expiry), val.earnings_date, val.earnings_report_time):
                    continue
                bpr = c.strike * 100.0 * 0.20 / 100.0  # ~20% of notional, per share
                ann_pct = (c.bid / max(0.01, bpr)) * (365.0 / c.dte) * 100.0
                if ann_pct < ann_min:
                    continue
                adv, bids = evaluate_strike_flow_prints(trades, "call", c.strike)
                if adv > 0:
                    continue
                ror = c.bid / max(0.01, bpr)
                row = _make_opportunity_row(
                    report_date, run_id, "SHORT_CALL", bucket, bundle, walls,
                    short_leg=c, leg2=None,
                    entry_credit=c.bid,
                    collateral=bpr,
                    max_loss=None,               # undefined risk
                    return_on_risk=ror,
                    annualized_pct=ann_pct,
                    break_even_low=None,
                    break_even_high=c.strike + c.bid,
                    safety_floor=None,
                    safety_ceiling=call_ceiling,
                    adverse_sweeps=adv, confirming_bids=bids,
                    sector_health=sector_health, market_regime=market_regime,
                    notes=f"Naked call >= ceiling {call_ceiling} (wall {walls.call_wall}); UNDEFINED RISK",
                )
                if best is None or row["confidence_score"] > best["confidence_score"]:
                    best = row
            if best:
                results.append(best)
            else:
                vetoes.append(f"{bucket}:SHORT_CALL:NO_QUALIFYING_STRIKE")

        # ---------------- BEAR_CALL_SPREAD ----------------
        bcs_trigger = (
            (verdict == "bearish" and (flow_c <= -FLOW_LEAN or bundle.dp_accumulating is False))
            or bundle.flow_label == "strong_bear"
            or (rsi >= 65.0 and flow_c <= 0.0)
        )
        if bear_veto:
            vetoes.append(f"{bucket}:BEAR_CALL_SPREAD:{bear_veto}")
        elif not walls.call_wall:
            vetoes.append(f"{bucket}:BEAR_CALL_SPREAD:NO_CALL_WALL")
        elif not bcs_trigger:
            vetoes.append(f"{bucket}:BEAR_CALL_SPREAD:NO_BEARISH_TRIGGER")
        elif not (ivr >= 25.0):
            vetoes.append(f"{bucket}:BEAR_CALL_SPREAD:IVR_LOW")
        else:
            best = None
            by_exp: dict[str, list[OptionContractQuote]] = {}
            for c in bucket_calls:
                by_exp.setdefault(c.expiry, []).append(c)
            # tech: prefer $5 verticals; other sectors: $10-wide; fallback to the other width
            for width in width_pref:
                for exp, exp_calls in by_exp.items():
                    if not is_earnings_clear(report_date, date.fromisoformat(exp), val.earnings_date, val.earnings_report_time):
                        continue
                    by_strike = {round(c.strike, 2): c for c in exp_calls}
                    for sc in exp_calls:
                        if sc.delta is None or not (SPREAD_DELTA_BAND[0] <= sc.delta <= SPREAD_DELTA_BAND[1]):
                            continue
                        if sc.strike < walls.call_wall:
                            continue
                        lc = by_strike.get(round(sc.strike + width, 2))
                        if not lc or lc.ask <= 0:
                            continue
                        credit = round(sc.bid - lc.ask, 2)   # NBBO is in pennies; round BEFORE the strict gate
                        if credit <= SPREAD_MIN_CREDIT:   # strict NBBO credit > $0.70
                            continue
                        max_loss = width - credit
                        if max_loss <= 0:
                            continue
                        ror = credit / max_loss
                        if ror < ROR_FLOOR_BY_WIDTH.get(width, SPREAD_ROR_BAND[0]):
                            continue
                        adv, bids = evaluate_strike_flow_prints(trades, "call", sc.strike)
                        if adv > 0:
                            continue
                        ann_pct = ror * (365.0 / sc.dte) * 100.0
                        row = _make_opportunity_row(
                            report_date, run_id, "BEAR_CALL_SPREAD", bucket, bundle, walls,
                            short_leg=sc, leg2=lc,
                            entry_credit=credit,
                            collateral=width,
                            max_loss=max_loss,
                            return_on_risk=ror,
                            annualized_pct=ann_pct,
                            break_even_low=None,
                            break_even_high=sc.strike + credit,
                            safety_floor=None,
                            safety_ceiling=walls.call_wall,
                            adverse_sweeps=adv, confirming_bids=bids,
                            sector_health=sector_health, market_regime=market_regime,
                            notes=f"BCS {width:.0f}-wide credit ${credit:.2f} > $0.70; short >= call wall {walls.call_wall}",
                        )
                        if best is None or row["confidence_score"] > best["confidence_score"]:
                            best = row
                if best is not None:
                    break
            if best:
                results.append(best)
            else:
                vetoes.append(f"{bucket}:BEAR_CALL_SPREAD:NO_QUALIFYING_SPREAD")

    results.sort(key=lambda r: r["confidence_score"], reverse=True)
    return results, vetoes


# ---------------------------------------------------------------------------
# Weekly Quota Planner (10 tickers x 2 contracts; 40% single-leg / 60% spreads)
# ---------------------------------------------------------------------------


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def row_collateral_usd(c: dict[str, Any]) -> float:
    """Collateral / BPR for ONE contract of a candidate row, in USD.

    SHORT_PUT: cash-secured strike · SHORT_CALL: ~20% of notional ·
    verticals: strike width (collateral column is per share; x100 for USD).
    """
    return round((c.get("collateral") or 0.0) * 100.0, 2)


def is_nontech_row(c: dict[str, Any]) -> bool:
    """True when the row's sector is a known non-tech sector ('Unknown' counts as neither)."""
    sec = c.get("sector") or "Unknown"
    return sec != "Unknown" and sec not in TECH_SECTORS


def plan_weekly_quota(
    candidates: list[dict[str, Any]],
    report_date: date,
    weekly_contracts: int = WEEKLY_CONTRACTS,
    max_tickers: int = WEEKLY_TICKERS,
    contracts_per_ticker: int = CONTRACTS_PER_TICKER,
    short_call_cap: int = SHORT_CALL_CAP,
    sector_cap: int = SECTOR_CAP,
    max_spread_contracts: int = MAX_SPREAD_CONTRACTS,
    collateral_cap: float = WEEKLY_COLLATERAL_CAP,
    credit_target: float = WEEKLY_CREDIT_TARGET,
    min_nontech_tickers: int = SECTOR_RESERVE_NONTECH,
) -> dict[str, Any]:
    """
    Two-phase deterministic fill:

    Phase A (pick rows): greedy by (confidence + regime tilt, credit desc, key asc)
    with hard caps — <= max_tickers symbols, <= contracts_per_ticker contracts per
    symbol, single-leg pool = round(40%), spread pool = the rest, SHORT_CALL <= cap,
    <= sector_cap tickers per sector ('Unknown' exempt), both DTE buckets forced in,
    every 1-contract pick must fit the remaining weekly collateral budget, and a
    sector reserve: while fewer than `min_nontech_tickers` non-tech tickers are
    planned, picks are drawn from the non-tech pool first (when it has eligible
    rows). The reserve never blocks the fill — when non-tech candidates run out,
    the remaining slots fill from the full pool and the shortfall is reported.

    Phase B (size spreads): spread orders are topped up round-robin (plan_rank order)
    up to `max_spread_contracts` contracts each while the quota and the collateral
    cap hold. Single legs stay at 1 contract (collateral-heavy). Unused single-leg
    slots spill over to the spread pool.

    Deterministic: ties broken by (adjusted score desc, credit desc, key asc).
    """
    for r in candidates:
        r["planned_contracts"] = 0
        r["plan_rank"] = None
        r["plan_score_adjusted"] = None
        r["plan_week"] = None

    single_pool = int(round(weekly_contracts * SINGLE_LEG_SHARE))
    spread_pool = weekly_contracts - single_pool

    state = {
        "single": 0, "spread": 0, "short_call": 0, "total": 0,
        "collateral": 0.0, "nontech_tickers": 0,
        "by_symbol": {}, "by_sector": {}, "by_bucket": {"LT8": 0, "DTE8_14": 0},
    }

    def adjusted(c: dict[str, Any]) -> float:
        score = (c.get("confidence_score") or 0.0) + regime_tilt(c["strategy"], c.get("market_regime") or "UNKNOWN")
        if sector_aligned(c):
            score += 1.5          # trade with the sector trend
        elif sector_trend_for(c) != "neutral":
            score -= 1.0          # against the sector trend (only possible when vetoes allowed it)
        return score

    picked: list[dict[str, Any]] = []
    tickers_used = 0

    def fits_collateral(c: dict[str, Any]) -> bool:
        return state["collateral"] + row_collateral_usd(c) <= collateral_cap + 1e-9

    def pick_from(pool: list[dict[str, Any]]) -> dict[str, Any] | None:
        # new tickers first (the plan should cover up to max_tickers names before
        # stacking more rows on an already-picked ticker), then adjusted score
        remaining = sorted(
            (c for c in pool if c["planned_contracts"] == 0),
            key=lambda c: (
                0 if c["symbol"] not in state["by_symbol"] else 1,
                -adjusted(c),
                -(c.get("entry_credit") or 0.0),
                c["opportunity_key"],
            ),
        )
        for c in remaining:
            sym = c["symbol"]
            sec = c.get("sector") or "Unknown"
            is_single = c["strategy"] in SINGLE_LEG_STRATEGIES
            if is_single and state["single"] >= single_pool:
                continue
            if not is_single and state["spread"] >= spread_pool:
                continue
            if c["strategy"] == "SHORT_CALL" and state["short_call"] >= short_call_cap:
                continue
            if state["by_symbol"].get(sym, 0) >= contracts_per_ticker:
                continue
            if sym not in state["by_symbol"]:
                if tickers_used >= max_tickers:
                    continue
                # sector cap binds only trend-CONTRARY/neutral picks; aligned picks are exempt
                if (
                    sec != "Unknown"
                    and not sector_aligned(c)
                    and state["by_sector"].get(sec, 0) >= sector_cap
                ):
                    continue
            if not fits_collateral(c):
                continue
            return c
        return None

    def commit(choice: dict[str, Any]) -> None:
        nonlocal tickers_used
        sym = choice["symbol"]
        sec = choice.get("sector") or "Unknown"
        is_single = choice["strategy"] in SINGLE_LEG_STRATEGIES
        if sym not in state["by_symbol"]:
            tickers_used += 1
            state["by_sector"][sec] = state["by_sector"].get(sec, 0) + 1
            if is_nontech_row(choice):
                state["nontech_tickers"] += 1
        state["by_symbol"][sym] = state["by_symbol"].get(sym, 0) + 1
        if is_single:
            state["single"] += 1
        else:
            state["spread"] += 1
        if choice["strategy"] == "SHORT_CALL":
            state["short_call"] += 1
        state["by_bucket"][choice["dte_bucket"]] = state["by_bucket"].get(choice["dte_bucket"], 0) + 1
        state["total"] += 1
        state["collateral"] = round(state["collateral"] + row_collateral_usd(choice), 2)
        choice["planned_contracts"] = 1
        choice["plan_rank"] = state["total"]
        choice["plan_score_adjusted"] = round(adjusted(choice), 2)
        choice["plan_week"] = iso_week_key(report_date)
        picked.append(choice)

    # ---- Phase A: pick rows (1 contract each) ----
    while state["total"] < weekly_contracts:
        reserve_unmet = state["nontech_tickers"] < min_nontech_tickers
        represented = {c["dte_bucket"] for c in candidates if c["planned_contracts"] == 0}
        empty = [b for b in DTE_BUCKETS if b in represented and state["by_bucket"].get(b, 0) == 0]
        choice = None
        if reserve_unmet:
            nontech_pool = [c for c in candidates if is_nontech_row(c)]
            if empty and state["total"] > 0:
                choice = pick_from([c for c in nontech_pool if c["dte_bucket"] in empty])
            if choice is None:
                choice = pick_from(nontech_pool)
        if choice is None:
            if empty and state["total"] > 0:
                choice = pick_from([c for c in candidates if c["dte_bucket"] in empty])
            if choice is None:
                choice = pick_from(candidates)
        if choice is None:
            break
        commit(choice)

    # ---- Phase B: size spread orders round-robin up to max_spread_contracts ----
    # unused single-leg slots spill into the spread budget so the 20-contract quota
    # can still fill when the single-leg pool runs dry
    spread_rows = [c for c in picked if c["strategy"] in SPREAD_STRATEGIES]
    spread_rows.sort(key=lambda c: c["plan_rank"] or 0)
    progressed = True
    while state["total"] < weekly_contracts and spread_rows and progressed:
        progressed = False
        for row in spread_rows:
            if state["total"] >= weekly_contracts:
                break
            if (row["planned_contracts"] or 0) >= max_spread_contracts:
                continue
            # per-TICKER total (a symbol with two spread rows must not exceed the cap either)
            if state["by_symbol"].get(row["symbol"], 0) >= contracts_per_ticker:
                continue
            if not fits_collateral(row):
                continue
            row["planned_contracts"] += 1
            state["spread"] += 1
            state["by_symbol"][row["symbol"]] = state["by_symbol"].get(row["symbol"], 0) + 1
            state["by_bucket"][row["dte_bucket"]] = state["by_bucket"].get(row["dte_bucket"], 0) + 1
            state["total"] += 1
            state["collateral"] = round(state["collateral"] + row_collateral_usd(row), 2)
            progressed = True

    projected_credit = round(sum((c.get("entry_credit") or 0.0) * 100.0 * (c.get("planned_contracts") or 0) for c in candidates), 2)
    plan_summary = {
        "plan_week": iso_week_key(report_date),
        "target_contracts": weekly_contracts,
        "planned_contracts": state["total"],
        "target_tickers": max_tickers,
        "planned_tickers": tickers_used,
        "single_leg_planned": state["single"],
        "single_leg_target": single_pool,
        "spread_planned": state["spread"],
        "spread_target": spread_pool,
        "spread_orders": len(spread_rows),
        "max_contracts_per_spread": max_spread_contracts,
        "short_call_planned": state["short_call"],
        "short_call_cap": short_call_cap,
        "bucket_split": dict(state["by_bucket"]),
        "sector_split": {k: v for k, v in state["by_sector"].items() if k != "Unknown"},
        "nontech_tickers_planned": state["nontech_tickers"],
        "min_nontech_tickers": min_nontech_tickers,
        "nontech_reserve_met": state["nontech_tickers"] >= min_nontech_tickers,
        "trend_aligned_contracts": sum(
            c["planned_contracts"] for c in picked if sector_aligned(c)
        ),
        "sector_cap": f"<= {sector_cap}/sector, trend-aligned picks exempt",
        "projected_weekly_credit_usd": projected_credit,
        "credit_target_usd": credit_target,
        "credit_gap_usd": round(credit_target - projected_credit, 2),
        "avg_credit_per_contract_usd": round(projected_credit / state["total"], 2) if state["total"] else 0.0,
        "weekly_collateral_usd": round(state["collateral"], 2),
        "collateral_cap_usd": collateral_cap,
        "collateral_headroom_usd": round(collateral_cap - state["collateral"], 2),
    }
    return {"summary": plan_summary, "picked": picked}


# ---------------------------------------------------------------------------
# Lifecycle Evaluator (50% profit, mid-cycle mark, bucket-wall breach, expiry)
# ---------------------------------------------------------------------------


def evaluate_open_opportunity_row(
    row: dict[str, Any],
    as_of_date: date,
    spot_price: float | None,
    short_quote: OptionContractQuote | None,
    leg2_quote: OptionContractQuote | None = None,
    current_bucket_put_wall: float | None = None,
    current_bucket_call_wall: float | None = None,
) -> dict[str, Any]:
    updated = dict(row)
    if updated.get("outcome") and updated.get("win") is not None:
        return updated

    strategy = str(updated.get("strategy") or "")
    exp_date = parse_iso_date(updated.get("short_expiry"))
    if not exp_date:
        return updated

    dte_now = (exp_date - as_of_date).days
    entry_credit = safe_float(updated.get("entry_credit"))
    short_strike = safe_float(updated.get("short_strike")) or 0.0
    leg2_strike = safe_float(updated.get("leg2_strike"))
    entry_dte = safe_int(updated.get("short_dte")) or 7

    close_mark: float | None = None
    if short_quote is not None:
        if strategy in SINGLE_LEG_STRATEGIES:
            close_mark = short_quote.ask
        elif strategy in SPREAD_STRATEGIES and leg2_quote is not None:
            close_mark = max(0.0, short_quote.ask - leg2_quote.bid)

    # 1. 50% profit auto-win
    if close_mark is not None and dte_now > 0 and entry_credit and entry_credit > 0:
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

    # 2. Mid-cycle mark (first pass at <= 50% of entry DTE remaining)
    half_dte = max(1, math.ceil(entry_dte / 2))
    if 0 < dte_now <= half_dte and not updated.get("midcycle_mark_date") and close_mark is not None:
        updated["midcycle_mark_date"] = as_of_date.isoformat()
        updated["midcycle_close_mark"] = round(close_mark, 4)
        if entry_credit is not None:
            updated["midcycle_pnl"] = round(entry_credit - close_mark, 4)

    # 3. Bucket wall breach defense
    if dte_now > 0 and not updated.get("defense_trigger_date"):
        if (
            strategy in {"SHORT_PUT", "BULL_PUT_SPREAD"}
            and current_bucket_put_wall is not None
            and short_strike > 0
            and current_bucket_put_wall < short_strike
        ):
            updated["defense_trigger_date"] = as_of_date.isoformat()
            updated["defense_reason"] = "BUCKET_PUT_WALL_BREACH"
        elif (
            strategy in {"SHORT_CALL", "BEAR_CALL_SPREAD"}
            and current_bucket_call_wall is not None
            and short_strike > 0
            and current_bucket_call_wall > short_strike
            and spot_price is not None
            and spot_price >= short_strike
        ):
            updated["defense_trigger_date"] = as_of_date.isoformat()
            updated["defense_reason"] = "BUCKET_CALL_WALL_BREACH"

    # 4. Expiry intrinsic settlement
    if dte_now <= 0 and spot_price is not None:
        if strategy == "SHORT_PUT":
            intrinsic = max(0.0, short_strike - spot_price)
            pnl = (entry_credit or 0.0) - intrinsic
        elif strategy == "SHORT_CALL":
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
        else:
            intrinsic = close_mark or 0.0
            pnl = intrinsic - (entry_credit or 0.0)

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
# External Clients
# ---------------------------------------------------------------------------


class UnusualWhalesClient:
    """
    Strict `unusual-whales-api` contract:
      - Base URL: https://api.unusualwhales.com
      - Headers: Authorization: Bearer <token> / UW-CLIENT-API-ID: 100001
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

    def get_interpolated_iv(self, ticker: str) -> list[dict[str, Any]]:
        res = self._get(f"/api/stock/{ticker}/interpolated-iv")
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
        min_dte: int = 1,
        max_dte: int = 14,
    ) -> list[OptionContractQuote]:
        """
        `/api/stock/{ticker}/option-contracts` caps each response at 500 rows across
        expiries, so: (1) discovery call, then (2) one call per expiry inside
        [min_dte, max_dte]. v2 only needs the short-DTE slice (<= 14 DTE).
        """
        base = {"exclude_zero_dte": "true"}
        discovery = self._fetch_contract_rows(ticker, base)
        expiries: set[str] = set()
        for row in discovery:
            parsed = parse_occ_symbol(str(row.get("option_symbol") or ""))
            if parsed:
                dte = (date.fromisoformat(parsed["expiry"]) - as_of_date).days
                if min_dte <= dte <= max_dte:
                    expiries.add(parsed["expiry"])
        merged: dict[str, dict[str, Any]] = {}
        for row in discovery:
            parsed = parse_occ_symbol(str(row.get("option_symbol") or ""))
            if parsed and min_dte <= (date.fromisoformat(parsed["expiry"]) - as_of_date).days <= max_dte:
                merged[str(row.get("option_symbol"))] = row
        for exp in sorted(expiries):
            for row in self._fetch_contract_rows(ticker, {**base, "expiry": exp}):
                merged[str(row.get("option_symbol"))] = row

        quotes: list[OptionContractQuote] = []
        for r in merged.values():
            occ = str(r.get("option_symbol") or "")
            parsed = parse_occ_symbol(occ)
            if not parsed:
                continue
            exp_date = parse_iso_date(parsed["expiry"])
            if not exp_date:
                continue
            dte = (exp_date - as_of_date).days
            if dte < 1:
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
                    gamma=safe_float(r.get("gamma")),
                    iv=safe_float(r.get("implied_volatility")),
                    open_interest=safe_float(r.get("open_interest")) or 0.0,
                )
            )
        return quotes

    def get_option_trades(self, ticker: str, limit: int = 120) -> list[dict[str, Any]]:
        res = self._get("/api/option-trades", params={"ticker_symbol": ticker, "limit": limit})
        return res if isinstance(res, list) else []


class SupabaseRestClient:
    """PostgREST wrapper for upstream signal tables and the v2 ledger."""

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
        opp_key = row["opportunity_key"]
        existing = self.select(
            table,
            params={
                "select": "opportunity_key,status,outcome,win,profit50_hit_date,midcycle_mark_date,exit_date",
                "opportunity_key": f"eq.{opp_key}",
            },
        )
        payload = dict(row)
        if existing:
            ex = existing[0]
            if ex.get("outcome") and ex.get("win") is not None:
                for k in ("status", "outcome", "win", "profit50_hit_date", "midcycle_mark_date", "exit_date"):
                    if ex.get(k) is not None:
                        payload[k] = ex[k]
            resp = self.session.patch(
                f"{self.base_url}/{table}",
                params={"opportunity_key": f"eq.{opp_key}"},
                data=json.dumps(payload, default=str),
                timeout=self.timeout,
            )
            resp.raise_for_status()
        else:
            resp = self.session.post(
                f"{self.base_url}/{table}",
                data=json.dumps(payload, default=str),
                timeout=self.timeout,
            )
            resp.raise_for_status()

    def upsert_sector_map(self, mappings: dict[str, dict[str, Any]]) -> None:
        if not mappings:
            return
        payload = [
            {"symbol": s, "sector": m["sector"], "source": m.get("source", "manual")}
            for s, m in mappings.items()
        ]
        resp = self.session.post(
            f"{self.base_url}/symbol_sector_map",
            params={"on_conflict": "symbol"},
            data=json.dumps(payload, default=str),
            timeout=self.timeout,
        )
        # 409/404 tolerated: sector cache is best-effort, never blocks the run
        if resp.status_code not in {200, 201, 204, 409, 404}:
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Universe Builder (from v1; never selects position/hedge)
# ---------------------------------------------------------------------------

US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "ARCA", "BATS", "CBOE", "IEX"}
CATEGORY_PRIORITY = {"core": 0, "satellite": 1, "watch1": 2, "watch2": 3, "watch3": 4, "euro": 5}


def build_watchlist_universe(
    rows: list[dict[str, Any]],
    override: list[str] | None = None,
    categories: tuple[str, ...] = WATCHLIST_CATEGORIES,
) -> list[tuple[str, str]]:
    by_symbol: dict[str, str] = {}
    for r in rows or []:
        sym = str(r.get("symbol") or "").strip().upper()
        cat = str(r.get("category") or "").strip().lower()
        exch = str(r.get("exchange") or "").strip().upper()
        if not sym or cat not in categories:
            continue
        if exch and exch not in US_EXCHANGES:
            continue
        if not is_us_option_symbol(sym):
            continue
        prev = by_symbol.get(sym)
        if prev is None or CATEGORY_PRIORITY.get(cat, 99) < CATEGORY_PRIORITY.get(prev, 99):
            by_symbol[sym] = cat
    universe = sorted(by_symbol.items(), key=lambda kv: (CATEGORY_PRIORITY.get(kv[1], 99), kv[0]))
    if override:
        allow = {s.strip().upper() for s in override if s.strip()}
        universe = [(s, c) for s, c in universe if s in allow]
    return universe


def us_market_date() -> date:
    """Latest plausible US trading day for a weekend run (Fri when Sat/Sun)."""
    today = date.today()
    if today.weekday() == 5:
        return today - timedelta(days=1)
    if today.weekday() == 6:
        return today - timedelta(days=2)
    return today


# ---------------------------------------------------------------------------
# Report Rendering
# ---------------------------------------------------------------------------


def _fmt(v: Any, nd: int = 2, dash: str = "—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown_report(
    report_date: date,
    candidates: list[dict[str, Any]],
    plan: dict[str, Any],
    regime: str,
    regime_note: str,
    sector_health: dict[str, dict[str, float | None]],
    lifecycle_updates: list[dict[str, Any]],
    dry_run: bool,
    universe_size: int,
    veto_counter: dict[str, int],
    categories_label: str = "core / satellite / watch1",
) -> str:
    s: list[str] = []
    ps = plan["summary"]
    picked = plan["picked"]

    s.append(f"# UW Option Income 2 — Short-Term Credit Board · {report_date.isoformat()}")
    s.append("")
    s.append(f"- Mode: {'**DRY RUN** (no Supabase writes)' if dry_run else 'LIVE'}")
    s.append(f"- Universe: {universe_size} watchlist symbols ({categories_label})")
    s.append(f"- Market regime: **{regime}** — {regime_note or 'n/a'}")
    s.append(
        f"- Weekly plan: **{ps['planned_contracts']}/{ps['target_contracts']} contracts** on "
        f"**{ps['planned_tickers']}/{ps['target_tickers']} tickers** · "
        f"single-leg {ps['single_leg_planned']}/{ps['single_leg_target']} · "
        f"spreads {ps['spread_planned']}/{ps['spread_target']} · "
        f"short call {ps['short_call_planned']}/{ps['short_call_cap']} (cap)"
    )
    s.append(
        f"- Projected weekly credit: **${ps['projected_weekly_credit_usd']:,.0f}** vs target "
        f"**${ps['credit_target_usd']:,.0f}** (gap ${ps['credit_gap_usd']:,.0f}, "
        f"avg ${ps['avg_credit_per_contract_usd']:,.0f}/contract)"
    )
    s.append(
        f"- Weekly collateral: **${ps['weekly_collateral_usd']:,.0f}** of ${ps['collateral_cap_usd']:,.0f} cap "
        f"(headroom ${ps['collateral_headroom_usd']:,.0f}) · spread orders sized up to "
        f"{ps['max_contracts_per_spread']}/order ({ps.get('spread_orders', 0)} orders)"
    )
    s.append(f"- DTE buckets: {ps['bucket_split']}")
    s.append("")

    # ---- Weekly plan table ----
    s.append("## Weekly Plan (quota fill order)")
    s.append("")
    if picked:
        s.append("| # | Symbol | Sector | Strategy | Bucket | Expiry | DTE | Short K | Long K | Cts | Credit | Credit $ | Collat $ | Conf | Wall (P/C) |")
        s.append("|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for c in sorted(picked, key=lambda r: r["plan_rank"] or 0):
            long_k = f"{c['leg2_strike']:.0f}" if c.get("leg2_strike") else "—"
            n = c.get("planned_contracts") or 0
            walls = f"{_fmt(c.get('bucket_put_wall'), 1)}/{_fmt(c.get('bucket_call_wall'), 1)}"
            s.append(
                f"| {c['plan_rank']} | **{c['symbol']}** | {c.get('sector') or 'Unknown'} | {c['strategy']} | "
                f"{c['dte_bucket']} | {c['short_expiry']} | {c['short_dte']} | {c['short_strike']:.0f} | {long_k} | "
                f"{n} | ${(c.get('entry_credit') or 0):.2f} | ${((c.get('entry_credit') or 0) * 100 * n):,.0f} | "
                f"${row_collateral_usd(c) * n:,.0f} | {c['confidence_score']:.0f} | {walls} |"
            )
        s.append("")
    else:
        s.append("_No qualified candidates reached the plan. See veto summary._")
        s.append("")

    # ---- Sector split ----
    if ps.get("sector_split"):
        s.append(f"Sector split: {', '.join(f'{k}×{v}' for k, v in sorted(ps['sector_split'].items()))}")
        s.append("")

    # ---- Full candidate board ----
    s.append("## Candidate Board (all qualified, incl. alternates)")
    s.append("")
    if candidates:
        s.append("| Symbol | Strategy | Bucket | Exp | DTE | Short K | Long K | Credit | RoR | Conf | Band | Flow | Sector RSI | Cautions |")
        s.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---|")
        for c in candidates:
            long_k = f"{c['leg2_strike']:.0f}" if c.get("leg2_strike") else "—"
            s.append(
                f"| {c['symbol']} | {c['strategy']} | {c['dte_bucket']} | {c['short_expiry']} | {c['short_dte']} | "
                f"{c['short_strike']:.0f} | {long_k} | ${(c.get('entry_credit') or 0):.2f} | "
                f"{_fmt((c.get('return_on_risk') or 0) * 100, 0)}% | {c['confidence_score']:.0f} | {c['score_band']} | "
                f"{c.get('flow_label') or '—'} | {_fmt(c.get('sector_health_rsi'), 0)} | {c.get('caution') or ''} |"
            )
        s.append("")
    else:
        s.append("_No candidates passed the hard gates._")
        s.append("")

    # ---- Sector health ----
    s.append("## Sector Health (scanned members)")
    s.append("")
    s.append("| Sector | Members | Mean RSI(14) | Mean Flow |")
    s.append("|---|---:|---:|---:|")
    for sec, h in sorted(sector_health.items(), key=lambda kv: -(kv[1].get("mean_rsi") or 50)):
        s.append(f"| {sec} | {h.get('members', 0)} | {_fmt(h.get('mean_rsi'), 1)} | {_fmt(h.get('mean_flow'), 1)} |")
    s.append("")

    # ---- Vetoes ----
    if veto_counter:
        s.append("## Veto Summary (top reasons)")
        s.append("")
        for reason, n in sorted(veto_counter.items(), key=lambda kv: -kv[1])[:20]:
            s.append(f"- `{reason}` × {n}")
        s.append("")

    # ---- Lifecycle ----
    if lifecycle_updates:
        s.append("## Lifecycle Updates (open ledger)")
        s.append("")
        for u in lifecycle_updates:
            bits = [u.get(k) for k in ("profit50_hit_date", "midcycle_mark_date", "defense_trigger_date", "exit_date") if u.get(k)]
            s.append(
                f"- {u.get('symbol')} {u.get('strategy')} {u.get('dte_bucket')}: "
                f"status={u.get('status')} outcome={u.get('outcome') or '—'} "
                f"{'/'.join(str(b) for b in bits)}"
            )
        s.append("")

    s.append("_Stateless engine: no brokerage holdings were read. SHORT_CALL rows are undefined-risk references._")
    return "\n".join(s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UW Option Income Engine v2 (short-term credit)")
    p.add_argument("--date", default=date.today().isoformat(), help="Report date YYYY-MM-DD")
    p.add_argument("--symbols", default="", help="Optional comma list narrowing the watchlist universe")
    p.add_argument(
        "--categories",
        default=",".join(WATCHLIST_CATEGORIES),
        help="Comma list of watchlist categories to scan (default core,satellite,watch1; "
             "use 'all' for every category incl. watch2/watch3/euro)",
    )
    p.add_argument("--min-score", type=float, default=60.0)
    p.add_argument("--top-n", type=int, default=40, help="Max candidate rows published to the board")
    p.add_argument("--weekly-contracts", type=int, default=WEEKLY_CONTRACTS)
    p.add_argument("--weekly-credit-target", type=float, default=WEEKLY_CREDIT_TARGET)
    p.add_argument("--max-tickers", type=int, default=WEEKLY_TICKERS)
    p.add_argument("--max-spread-contracts", type=int, default=MAX_SPREAD_CONTRACTS,
                   help="Max contracts per spread order (round-robin sized, default 5)")
    p.add_argument("--max-weekly-collateral", type=float, default=WEEKLY_COLLATERAL_CAP,
                   help="Max total weekly collateral / BPR in USD (default 100000)")
    p.add_argument("--dry-run", action="store_true", help="Write reports only; no Supabase row writes")
    p.add_argument("--skip-tracking", action="store_true", help="Skip the open-ledger lifecycle pass")
    p.add_argument("--output-dir", default="reports")
    p.add_argument("--table", default="uw-option-income2")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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
    if not (sb_url and sb_key):
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.", file=sys.stderr)
        return 2

    uw = UnusualWhalesClient(api_key=uw_key)
    sb = SupabaseRestClient(url=sb_url, key=sb_key)
    run_id = f"uw-oi2-{report_date.isoformat()}-{int(time.time())}"

    raw_categories = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    if raw_categories == ["all"]:
        scan_categories = ALL_WATCHLIST_CATEGORIES
        sb_category_filter = None
    else:
        unknown = [c for c in raw_categories if c not in ALL_WATCHLIST_CATEGORIES]
        if unknown:
            print(f"ERROR: unknown categories {unknown}; valid: {list(ALL_WATCHLIST_CATEGORIES)} or 'all'.", file=sys.stderr)
            return 2
        scan_categories = tuple(raw_categories)
        sb_category_filter = ",".join(scan_categories)

    wl_params: dict[str, str] = {"select": "symbol,category,exchange"}
    if sb_category_filter:
        wl_params["category"] = f"in.({sb_category_filter})"
    wl_rows = sb.select("watchlist", params=wl_params)
    override = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    symbols = build_watchlist_universe(wl_rows, override, categories=scan_categories)
    print(f"Universe: {len(symbols)} watchlist symbols ({', '.join(scan_categories)})")

    # Market regime: run-level note on the latest trade-jlaw-v2 rows
    regime_note_rows = sb.select(
        "trade-jlaw-v2",
        params={"select": "run_date,regime", "order": "run_date.desc", "limit": "1"},
    )
    regime_note = str(regime_note_rows[0].get("regime") or "") if regime_note_rows else ""
    market_regime = classify_market_regime(regime_note)
    print(f"Market regime: {market_regime} ({regime_note[:80] or 'no jlaw regime row'})")

    # Sector overrides from the persistent cache, then the curated seed
    sector_overrides: dict[str, str] = {}
    for r in sb.select("symbol_sector_map", params={"select": "symbol,sector", "limit": "1000"}):
        sector_overrides[str(r.get("symbol") or "").upper()] = str(r.get("sector") or "Unknown")

    # Prefetch flow-signal history once for the whole universe (persistence leg)
    flow_hist_by_sym: dict[str, list[dict[str, Any]]] = {}
    sym_list = [s for s, _ in symbols]
    for i in range(0, len(sym_list), 80):
        chunk = sym_list[i : i + 80]
        rows = sb.select(
            "option_flow_symbol_signals",
            params={
                "select": "ticker,signal_date,net_directional_premium,unusual_premium,directional_confidence",
                "ticker": f"in.({','.join(chunk)})",
                "signal_date": f"gte.{(report_date - timedelta(days=10)).isoformat()}",
                "order": "signal_date.desc",
            },
        )
        for r in rows:
            flow_hist_by_sym.setdefault(str(r.get("ticker") or "").upper(), []).append(r)

    all_candidates: list[dict[str, Any]] = []
    veto_counter: dict[str, int] = {}
    bundles: list[SymbolSignalBundle] = []
    chain_cache: dict[str, list[OptionContractQuote]] = {}
    trades_cache: dict[str, list[dict[str, Any]]] = {}
    spot_cache: dict[str, float] = {}
    walls_cache: dict[str, dict[str, BucketWalls]] = {}

    t0 = time.time()
    for idx, (sym, cat) in enumerate(symbols, 1):
        try:
            earnings_rows = uw.get_earnings(sym)
            iv_rows = uw.get_interpolated_iv(sym)
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
                sector=sector_overrides.get(sym) or SECTOR_SEED.get(sym, "Unknown"),
            )

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
                if not bundle.spot_price:
                    bundle.spot_price = safe_float(gr.get("spot_price"))

            fv = sb.select(
                "flow_verdict_rows",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "updated_at.desc", "limit": "1"},
            )
            next_earn_override: str | None = None
            if fv:
                fr = fv[0]
                bundle.verdict = str(fr.get("verdict") or "no-data")
                bundle.verdict_reasons = list(fr.get("reasons") or []) + list(fr.get("caveats") or [])
                bundle.flow_alert_count = safe_int(fr.get("alert_count"))
                bundle.verdict_date = str(fr.get("verdict_date") or "")[:10] or None
                bundle.alert_count_total = safe_int(fr.get("alert_count_total"))
                bundle.alert_premium_tilt = safe_float(fr.get("premium_tilt"))
                bundle.verdict_quant = fr.get("verdict_quant")
                if verdict_is_stale(bundle.verdict_date, report_date):
                    bundle.verdict_reasons.insert(0, f"VERDICT_STALE: {bundle.verdict} as of {bundle.verdict_date}")
                    bundle.verdict = "no-data"
                    bundle.verdict_stale = True
                if fr.get("next_earnings_date"):
                    next_earn_override = str(fr.get("next_earnings_date"))[:10]

            flow_sig = [
                r for r in flow_hist_by_sym.get(sym, [])
                if str(r.get("signal_date") or "") <= report_date.isoformat()
            ]
            bundle.flow_history = flow_sig[:3]
            if flow_sig:
                bundle.flow_signal_date = flow_sig[0].get("signal_date")

            dp = sb.select(
                "darkpool_symbol_daily",
                params={"select": "*", "symbol": f"eq.{sym}", "order": "fetch_date.desc", "limit": "1"},
            )
            if dp:
                dr = dp[0]
                bundle.dp_as_of = dr.get("window_end") or dr.get("fetch_date")
                bundle.dp_total_premium_mm = round((safe_float(dr.get("total_premium")) or 0.0) / 1e6, 2)
                bundle.dp_net_mm = round((safe_float(dr.get("net_premium")) or 0.0) / 1e6, 2)
                bundle.dp_top_level = safe_float(dr.get("top_level"))
                bundle.dp_top_level_mm = round((safe_float(dr.get("top_level_premium")) or 0.0) / 1e6, 2)
                bundle.dp_accumulating = bool(dr.get("accumulating")) if dr.get("accumulating") is not None else None
                if not bundle.spot_price and bundle.dp_top_level:
                    bundle.spot_price = bundle.dp_top_level

            trades = uw.get_option_trades(sym, limit=120)
            if not bundle.spot_price and trades:
                for t in trades:
                    u_px = safe_float(t.get("underlying_price"))
                    if u_px and u_px > 0:
                        bundle.spot_price = u_px
                        break

            if not bundle.spot_price:
                veto_counter[f"{sym}:NO_SPOT"] = veto_counter.get(f"{sym}:NO_SPOT", 0) + 1
                continue

            compute_flow_composite(bundle, report_date)
            val_ctx = compute_uw_valuation_context(sym, bundle.spot_price, earnings_rows, as_of_date=report_date)
            if not val_ctx.earnings_date and next_earn_override:
                val_ctx.earnings_date = next_earn_override
            bundle.valuation = val_ctx
            bundles.append(bundle)

            chain = uw.get_option_contracts(sym, as_of_date=report_date)
            chain_cache[sym] = chain
            trades_cache[sym] = trades
            spot_cache[sym] = bundle.spot_price
            walls_by_bucket = {
                b: compute_bucket_walls(chain, bundle.spot_price, b, bundle.gex_put_wall, bundle.gex_call_wall)
                for b in DTE_BUCKETS
            }
            walls_cache[sym] = walls_by_bucket
        except requests.RequestException as e:
            veto_counter[f"{sym}:UW_ERROR"] = veto_counter.get(f"{sym}:UW_ERROR", 0) + 1
            print(f"  [{idx}/{len(symbols)}] {sym}: UW request failed: {e}", file=sys.stderr)
            continue

        if idx % 20 == 0 or idx == len(symbols):
            print(f"  [{idx}/{len(symbols)}] scanned ({time.time() - t0:.0f}s elapsed)", flush=True)

    # Sector health aggregated over everything scanned (with or without candidates)
    sector_health = aggregate_sector_health(bundles)

    # Second pass: generate candidates now that sector health is known
    for bundle in bundles:
        sym_opps, sym_vetoes = generate_opportunities_for_symbol(
            report_date,
            run_id,
            bundle,
            chain_cache.get(bundle.symbol, []),
            walls_cache.get(bundle.symbol, {}),
            option_trades=trades_cache.get(bundle.symbol),
            sector_health=sector_health,
            market_regime=market_regime,
        )
        all_candidates.extend(sym_opps)
        for v in sym_vetoes:
            key = v.split(":", 2)[-1] if ":" in v else v
            veto_counter[key] = veto_counter.get(key, 0) + 1

    # Lifecycle pass on open rows
    lifecycle_updates: list[dict[str, Any]] = []
    if not args.skip_tracking:
        open_rows = sb.select(
            args.table,
            params={"select": "*", "status": "in.(PROPOSED,ACTIVE)", "report_date": f"lt.{report_date.isoformat()}"},
        )
        for row in open_rows:
            sym = str(row.get("symbol") or "").upper()
            if sym not in chain_cache:
                try:
                    chain_cache[sym] = uw.get_option_contracts(sym, as_of_date=report_date)
                except requests.RequestException:
                    chain_cache[sym] = []
            walls = walls_cache.get(sym) or {
                b: compute_bucket_walls(chain_cache.get(sym, []), spot_cache.get(sym), b) for b in DTE_BUCKETS
            }
            bucket = str(row.get("dte_bucket") or "LT8")
            bw = walls.get(bucket) or BucketWalls(bucket=bucket)
            by_occ = {c.option_symbol: c for c in chain_cache.get(sym, [])}
            updated = evaluate_open_opportunity_row(
                row,
                as_of_date=report_date,
                spot_price=spot_cache.get(sym),
                short_quote=by_occ.get(str(row.get("short_option_symbol") or "")),
                leg2_quote=by_occ.get(str(row.get("leg2_option_symbol") or "")),
                current_bucket_put_wall=bw.put_wall,
                current_bucket_call_wall=bw.call_wall,
            )
            changed = {k: v for k, v in updated.items() if row.get(k) != v}
            if changed:
                lifecycle_updates.append(updated)
                if not args.dry_run:
                    sb.patch_row(args.table, row["opportunity_key"], changed)

    qualified = [r for r in all_candidates if (r.get("confidence_score") or 0.0) >= args.min_score]
    qualified.sort(key=lambda r: (-(r.get("confidence_score") or 0.0), r["opportunity_key"]))
    published = qualified[: args.top_n]

    # Weekly quota plan over the qualified pool
    plan = plan_weekly_quota(
        published,
        report_date,
        weekly_contracts=args.weekly_contracts,
        max_tickers=args.max_tickers,
        max_spread_contracts=args.max_spread_contracts,
        collateral_cap=args.max_weekly_collateral,
        credit_target=args.weekly_credit_target,
    )

    if not args.dry_run:
        for row in published:
            sb.upsert_opportunity(args.table, row)
        # persist curated sector seed for anything the cache is missing
        missing = {
            s: {"sector": SECTOR_SEED[s], "source": "manual"}
            for s, _ in symbols
            if s in SECTOR_SEED and s not in sector_overrides
        }
        sb.upsert_sector_map(missing)

    md_text = render_markdown_report(
        report_date, published, plan, market_regime, regime_note,
        sector_health, lifecycle_updates, args.dry_run, len(symbols), veto_counter,
        categories_label=" / ".join(scan_categories),
    )
    md_path = out_dir / f"uw_option_income2_{report_date.isoformat()}.md"
    json_path = out_dir / f"uw_option_income2_{report_date.isoformat()}.json"
    md_path.write_text(md_text, encoding="utf-8")
    payload = {
        "run_id": run_id,
        "report_date": report_date.isoformat(),
        "dry_run": args.dry_run,
        "market_regime": market_regime,
        "regime_note": regime_note,
        "universe_size": len(symbols),
        "weekly_plan": plan["summary"],
        "candidates": published,
        "sector_health": sector_health,
        "lifecycle_updates": lifecycle_updates,
        "veto_counter": veto_counter,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    ps = plan["summary"]
    print(
        f"Wrote {len(published)} qualified candidates; plan {ps['planned_contracts']}/{ps['target_contracts']} contracts "
        f"on {ps['planned_tickers']} tickers, projected credit ${ps['projected_weekly_credit_usd']:,.0f} "
        f"(target ${ps['credit_target_usd']:,.0f})"
    )
    print(f"Reports: {md_path} / {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
