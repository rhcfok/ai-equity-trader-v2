#!/usr/bin/env python3
"""Deterministic CSP and 10-point bull-put-spread research screening core.

Network access remains outside this module.  The caller supplies point-in-time
option chains plus J Law, Quant2, GEX, earnings, volatility, and flow context.
The module applies hard gates first, then assigns a transparent provisional
0-100 confidence score.  It never creates an order or an order instruction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
import re
from typing import Any, Iterable


class ScreeningError(ValueError):
    """Raised when supplied market inputs are malformed."""


@dataclass(frozen=True)
class ScreeningConfig:
    """Explicit strategy policy and score thresholds."""

    csp_min_dte: int = 3
    csp_max_dte: int = 10
    csp_short_delta_max: float = -0.20  # Strictly less than this signed value.
    bps_min_dte: int = 7
    bps_max_dte: int = 30
    bps_width: float = 10.0
    bps_min_return_on_risk: float = 0.20
    bps_max_return_on_risk: float = 0.30
    min_open_interest: int = 100
    max_bid_ask_spread_pct: float = 0.25
    contract_multiplier: int = 100
    require_earnings_date: bool = True
    include_watch: bool = True
    rank_limit: int = 20


US_EXCHANGES = {
    "NASDAQ", "NASDAQGS", "NASDAQCM", "NASDAQGM", "NYSE", "NYSEARCA",
    "NYSE AMERICAN", "NYSE MKT", "AMEX", "ARCA", "BATS", "CBOE", "IEX",
    "US", "USA",
}
_OCC_RE = re.compile(r"(\d{6})([CP])(\d{8})$")


def number(value: Any) -> float | None:
    """Return a finite float or None without accepting booleans as numeric input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if isfinite(output) else None


def as_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def occ_parts(option_symbol: Any) -> tuple[date | None, str | None, float | None]:
    """Extract expiry, type, and strike from a standard OCC option symbol."""
    match = _OCC_RE.search(str(option_symbol or "").strip().upper())
    if not match:
        return None, None, None
    yy_mm_dd, option_type, strike_millis = match.groups()
    try:
        expiry = date.fromisoformat(f"20{yy_mm_dd[:2]}-{yy_mm_dd[2:4]}-{yy_mm_dd[4:6]}")
        return expiry, "put" if option_type == "P" else "call", int(strike_millis) / 1000.0
    except ValueError:
        return None, None, None


def normalize_contract(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize provider aliases while failing closed on essential mechanics."""
    option_symbol = str(field(row, "option_symbol", "symbol", "contract_symbol") or "")
    occ_expiry, occ_type, occ_strike = occ_parts(option_symbol)
    option_type = str(field(row, "option_type", "type") or occ_type or "").lower()
    strike = number(field(row, "strike"))
    expiry = as_iso_date(field(row, "expires", "expiry", "expiration"))
    if strike is None:
        strike = occ_strike
    if expiry is None:
        expiry = occ_expiry
    delta = number(field(row, "delta"))
    if option_type not in {"put", "call"} or strike is None or expiry is None or delta is None:
        return None
    return {
        "option_symbol": option_symbol,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry.isoformat(),
        "delta": delta,
        "open_interest": number(field(row, "open_interest", "oi")),
        "volume": number(field(row, "volume")),
        "iv": number(field(row, "iv", "implied_volatility")),
        "bid": number(field(row, "bid", "bid_price", "nbbo_bid")),
        "ask": number(field(row, "ask", "ask_price", "nbbo_ask")),
        "last": number(field(row, "last_price", "last")),
        "theo": number(field(row, "theo", "theoretical_price")),
        "last_tape_time": field(row, "last_tape_time", "updated_at", "timestamp"),
    }


def parse_chain_payload(payload: Any) -> tuple[float | None, list[dict[str, Any]], date | None]:
    """Extract spot, contracts, and provider market date from common source shapes."""
    if isinstance(payload, list):
        raw_rows = payload
        payload_dict: dict[str, Any] = {}
    elif isinstance(payload, dict):
        payload_dict = payload
        raw_rows = payload.get("states") or payload.get("data") or payload.get("chains") or []
    else:
        raise ScreeningError("Option-chain payload must be a JSON object or list.")
    if not isinstance(raw_rows, list):
        raise ScreeningError("Option-chain payload contains no row list.")
    rows = [normalized for raw in raw_rows if isinstance(raw, dict) if (normalized := normalize_contract(raw))]
    price_data = payload_dict.get("price_data") if isinstance(payload_dict.get("price_data"), dict) else {}
    spot = number(field(price_data, "price", "last", "midpoint"))
    if spot is None:
        spot = number(field(payload_dict, "price", "spot", "underlying_price"))
    tape_dates = [as_iso_date(row.get("last_tape_time")) for row in rows]
    current_dates = [item for item in tape_dates if item]
    return spot, rows, max(current_dates) if current_dates else None


def is_us_exchange(exchange: Any) -> bool:
    value = str(exchange or "").strip().upper()
    return value in US_EXCHANGES or value.startswith("NASDAQ") or value.startswith("NYSE")


def quote_spread_pct(contract: dict[str, Any]) -> float | None:
    bid, ask = number(contract.get("bid")), number(contract.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    return (ask - bid) / midpoint if midpoint > 0 else None


def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def technical_safety_floor(context: dict[str, Any]) -> tuple[float | None, str]:
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    gex = context.get("gex") if isinstance(context.get("gex"), dict) else {}
    stop = number(jlaw.get("stop_loss"))
    put_wall = number(gex.get("put_wall"))
    regime = str(gex.get("regime") or "").lower()
    if stop is None or stop <= 0:
        return None, "JLAW_STOP_MISSING"
    if put_wall is not None and put_wall > 0 and regime not in {"sub_resolution", "", "unknown"}:
        return min(stop, put_wall), "JLAW_AND_GEX"
    return stop, "JLAW_ONLY_GEX_MAP" if put_wall is not None else "JLAW_ONLY"


def quant_alignment_score(quant: dict[str, Any] | None) -> int:
    """Retain the prior compact score for backwards-compatible ledger field use."""
    quant = quant or {}
    output = 0
    rank = number(quant.get("rank"))
    active = number(quant.get("n_active_rules"))
    frequency = number(quant.get("score_hist_freq"))
    if rank is not None and rank <= 50:
        output += 2
    elif rank is not None and rank <= 100:
        output += 1
    if active is not None and active >= 4:
        output += 1
    if frequency is not None and frequency >= 0.55:
        output += 1
    return output


def alignment_score(context: dict[str, Any]) -> int:
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    gex = context.get("gex") if isinstance(context.get("gex"), dict) else {}
    score = 2 if str(jlaw.get("classification") or "").lower() == "valid" else 0
    score += quant_alignment_score(context.get("quant") if isinstance(context.get("quant"), dict) else None)
    regime = str(gex.get("regime") or "").lower()
    if regime == "positive":
        score += 1
    elif regime == "negative":
        score -= 1
    iv_rank = number(jlaw.get("option_iv_rank"))
    if iv_rank is not None and iv_rank >= 30:
        score += 1
    return score


def earnings_status(context: dict[str, Any], expiry: date, require_date: bool) -> tuple[bool, str, str | None]:
    earnings = context.get("earnings") if isinstance(context.get("earnings"), dict) else {}
    event_date = as_iso_date(field(earnings, "date", "earnings_date", "report_date"))
    if event_date is None:
        return (not require_date, "EARNINGS_UNCONFIRMED", None)
    if event_date <= expiry:
        return False, "EARNINGS_OVERLAP", event_date.isoformat()
    return True, "EARNINGS_CLEAR", event_date.isoformat()


def base_context_gates(context: dict[str, Any], config: ScreeningConfig) -> tuple[bool, list[str], float | None, str]:
    reasons: list[str] = []
    symbol = str(context.get("symbol") or "").upper()
    if not symbol:
        return False, ["SYMBOL_MISSING"], None, "NONE"
    if not is_us_exchange(context.get("exchange")):
        reasons.append("NON_US_OR_UNCONFIRMED_EXCHANGE")
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    allowed = {"valid"} | ({"watch"} if config.include_watch else set())
    if str(jlaw.get("classification") or "").lower() not in allowed:
        reasons.append("JLAW_CLASSIFICATION_NOT_ALLOWED")
    floor, floor_source = technical_safety_floor(context)
    if floor is None:
        reasons.append("JLAW_STOP_MISSING")
    return not reasons, reasons, floor, floor_source


def contract_gates(
    contract: dict[str, Any], *, spot: float, floor: float, as_of: date,
    min_dte: int, max_dte: int, short_delta_max: float | None, config: ScreeningConfig,
) -> tuple[bool, list[str], int]:
    """Apply short-put gates; a None delta threshold intentionally skips delta."""
    reasons: list[str] = []
    expiry = as_iso_date(contract.get("expiry"))
    if expiry is None:
        return False, ["EXPIRY_MISSING"], -1
    dte = (expiry - as_of).days
    if contract.get("option_type") != "put":
        reasons.append("NOT_PUT")
    if dte < min_dte or dte > max_dte:
        reasons.append("DTE_OUTSIDE_WINDOW")
    strike = number(contract.get("strike"))
    if strike is None or strike >= spot:
        reasons.append("NOT_OTM")
    if strike is None or strike > floor:
        reasons.append("ABOVE_TECHNICAL_SAFETY_FLOOR")
    if short_delta_max is not None:
        delta = number(contract.get("delta"))
        # User policy is strict: delta must be < -0.20, not <= -0.20.
        if delta is None or delta >= short_delta_max:
            reasons.append("CSP_DELTA_NOT_STRICTLY_BELOW_THRESHOLD")
    oi = number(contract.get("open_interest"))
    if oi is None or oi < config.min_open_interest:
        reasons.append("OPEN_INTEREST_BELOW_MINIMUM")
    spread_pct = quote_spread_pct(contract)
    if spread_pct is None:
        reasons.append("NBBO_QUOTE_MISSING")
    elif spread_pct > config.max_bid_ask_spread_pct:
        reasons.append("BID_ASK_SPREAD_TOO_WIDE")
    return not reasons, reasons, dte


def context_brief(context: dict[str, Any], floor: float, floor_source: str) -> str:
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    quant = context.get("quant") if isinstance(context.get("quant"), dict) else {}
    gex = context.get("gex") if isinstance(context.get("gex"), dict) else {}
    parts = [f"JLaw {jlaw.get('classification', 'unknown')}", f"floor ${floor:.2f} ({floor_source})"]
    if quant.get("rank") is not None:
        parts.append(f"Quant2 rank {quant['rank']}")
    iv_rank = number(jlaw.get("option_iv_rank"))
    if iv_rank is not None:
        parts.append(f"IV Rank {iv_rank:.1f}")
    if gex.get("regime"):
        parts.append(f"GEX {gex['regime']}")
    return "; ".join(parts)


def _base_candidate(strategy: str, context: dict[str, Any], contract: dict[str, Any], dte: int,
                    floor: float, floor_source: str, earnings_date: str | None) -> dict[str, Any]:
    source_context = {
        "jlaw": context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {},
        "quant": context.get("quant") if isinstance(context.get("quant"), dict) else {},
        "gex": context.get("gex") if isinstance(context.get("gex"), dict) else {},
        "live_gex": context.get("live_gex") if isinstance(context.get("live_gex"), dict) else {},
        "earnings": context.get("earnings") if isinstance(context.get("earnings"), dict) else {},
        "term_structure": context.get("term_structure") if isinstance(context.get("term_structure"), dict) else {},
        "flow_summary": context.get("flow_summary") if isinstance(context.get("flow_summary"), dict) else {},
    }
    return {
        "strategy": strategy, "symbol": str(context.get("symbol") or "").upper(),
        "category": context.get("category"), "exchange": context.get("exchange"),
        "expiry": contract["expiry"], "dte": dte, "short_put": contract,
        "safety_floor": round(floor, 4), "safety_floor_source": floor_source,
        "earnings_date": earnings_date, "alignment_score": alignment_score(context),
        "context_brief": context_brief(context, floor, floor_source),
        "source_context": source_context,
    }


def _jlaw_score_points(jlaw_score: Any) -> float:
    value = number(jlaw_score)
    return 0.0 if value is None else 5.0 * clip(value / 15.0)


def _quant_points(quant: dict[str, Any]) -> float:
    rank = number(quant.get("rank"))
    rank_points = 0.0 if rank is None else 10.0 if rank <= 20 else 8.0 if rank <= 50 else 5.0 if rank <= 100 else 2.0
    active = number(quant.get("n_active_rules"))
    frequency = number(quant.get("score_hist_freq"))
    return rank_points + 5.0 * clip((active or 0.0) / 5.0) + 5.0 * clip((frequency or 0.0) / 0.60)


def _gex_points(candidate: dict[str, Any], context: dict[str, Any], spot: float) -> float:
    gex = context.get("gex") if isinstance(context.get("gex"), dict) else {}
    regime = str(gex.get("regime") or "").lower()
    put_wall = number(gex.get("put_wall"))
    if regime in {"sub_resolution", "", "unknown"} or put_wall is None:
        return 0.0
    regime_points = 5.0 if regime == "positive" else 2.0 if regime == "neutral" else 0.0
    reference = number(candidate.get("break_even")) if candidate.get("strategy") == "BPS" else number((candidate.get("short_put") or {}).get("strike"))
    support_points = 5.0 * clip(((put_wall or 0.0) - (reference or spot)) / spot / 0.02)
    live = context.get("live_gex") if isinstance(context.get("live_gex"), dict) else {}
    live_wall = number(live.get("put_wall"))
    agreement = 5.0 if live_wall is not None and abs(live_wall - put_wall) <= 2.5 else 0.0
    return regime_points + support_points + agreement


def _volatility_points(candidate: dict[str, Any], context: dict[str, Any]) -> float:
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    iv_rank = number(jlaw.get("option_iv_rank"))
    iv_rank_points = 6.0 if iv_rank is not None and 40 <= iv_rank <= 80 else 3.0 if iv_rank is not None and 25 <= iv_rank < 40 or iv_rank is not None and 80 < iv_rank <= 90 else 0.0
    iv30, rv20 = number(jlaw.get("option_iv30")), number(jlaw.get("option_rv20"))
    ratio = iv30 / rv20 if iv30 and rv20 and rv20 > 0 else None
    premium_points = 5.0 if ratio is not None and 1.05 <= ratio <= 1.75 else 2.0 if ratio is not None and 0.95 <= ratio < 1.05 else 1.0 if ratio is not None and 1.75 < ratio <= 2.0 else 0.0
    term = context.get("term_structure") if isinstance(context.get("term_structure"), dict) else {}
    status = str(term.get("status") or "").lower()
    term_points = 4.0 if status == "normal" else 1.0 if status == "backwardation" else 0.0
    return iv_rank_points + premium_points + term_points


def _liquidity_points(candidate: dict[str, Any]) -> float:
    legs = [candidate.get("short_put") or {}]
    if candidate.get("strategy") == "BPS":
        legs.append(candidate.get("long_put") or {})
    oi = min((number(leg.get("open_interest")) or 0.0) for leg in legs)
    spread = max((quote_spread_pct(leg) if quote_spread_pct(leg) is not None else 1.0) for leg in legs)
    volume = min((number(leg.get("volume")) or 0.0) for leg in legs)
    oi_points = 4.0 if oi >= 1000 else 3.0 if oi >= 500 else 2.0 if oi >= 250 else 1.0
    spread_points = 4.0 if spread <= 0.05 else 3.0 if spread <= 0.10 else 2.0 if spread <= 0.15 else 1.0
    volume_points = 2.0 if volume >= 100 else 1.0 if volume >= 25 else 0.0
    return oi_points + spread_points + volume_points


def _reward_points(candidate: dict[str, Any], config: ScreeningConfig) -> float:
    metric = number(candidate.get("reward_metric")) or 0.0
    if candidate.get("strategy") == "CSP":
        return 10.0 * clip(metric / 0.03)
    midpoint = (config.bps_min_return_on_risk + config.bps_max_return_on_risk) / 2.0
    half_band = (config.bps_max_return_on_risk - config.bps_min_return_on_risk) / 2.0
    return 10.0 - 3.0 * abs(metric - midpoint) / half_band if half_band > 0 else 0.0


def _flow_oi_points(context: dict[str, Any]) -> float:
    flow = context.get("flow_summary") if isinstance(context.get("flow_summary"), dict) else {}
    if not flow:
        return 0.0
    if bool(flow.get("adverse_near_strike")):
        return 0.0
    oi = str(flow.get("oi_confirmation") or "").lower()
    oi_points = 4.0 if oi == "confirmed" else 2.0 if oi == "stable" else 0.0
    # Absence of a flow flag is not evidence of benign positioning. Award the
    # no-adverse points only after the caller explicitly classifies the review.
    if str(flow.get("review_status") or "").lower() != "clear":
        return oi_points
    return oi_points + 3.0 + 3.0  # explicit clear review, no adverse nearby flags


def apply_confidence_score(candidate: dict[str, Any], context: dict[str, Any], config: ScreeningConfig) -> dict[str, Any]:
    """Attach an auditable score to a hard-pass candidate; no probability is implied."""
    spot = number(candidate.get("spot")) or 0.0
    short = candidate.get("short_put") if isinstance(candidate.get("short_put"), dict) else {}
    floor = number(candidate.get("safety_floor")) or 0.0
    short_strike = number(short.get("strike")) or 0.0
    jlaw = context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {}
    technical = 5.0 + _jlaw_score_points(jlaw.get("jlaw_score"))
    reference = number(candidate.get("break_even")) if candidate.get("strategy") == "BPS" else short_strike
    technical += 5.0 * clip((floor - (reference or spot)) / spot / 0.03) if spot > 0 else 0.0
    if candidate.get("strategy") == "CSP":
        abs_delta = abs(number(short.get("delta")) or 1.0)
        delta_points = 5.0 if 0.20 < abs_delta <= 0.30 else 5.0 * clip((0.50 - abs_delta) / 0.20)
        technical += delta_points
    else:
        technical += 5.0 * clip((spot - short_strike) / spot / 0.025) if spot > 0 else 0.0
    quant = context.get("quant") if isinstance(context.get("quant"), dict) else {}
    components = {
        "jlaw_technical": round(technical, 2),
        "quant2": round(_quant_points(quant), 2),
        "gex": round(_gex_points(candidate, context, spot), 2),
        "volatility_event": round(_volatility_points(candidate, context), 2),
        "liquidity_execution": round(_liquidity_points(candidate), 2),
        "reward": round(_reward_points(candidate, config), 2),
        "flow_oi": round(_flow_oi_points(context), 2),
    }
    raw = sum(components.values())
    flow = context.get("flow_summary") if isinstance(context.get("flow_summary"), dict) else {}
    capped = min(raw, 69.0) if bool(flow.get("adverse_near_strike")) else raw
    candidate["score_components"] = components
    candidate["confidence_score"] = int(round(min(100.0, capped)))
    candidate["score_band"] = score_band(candidate["confidence_score"])
    candidate["score_version"] = "csp_bps_v3_ranked_watch_2026-10-16"
    candidate["flow_review_flag"] = bool(flow.get("adverse_near_strike"))
    candidate["candidate_status"] = "ELIGIBLE"
    candidate["reason"] = f"Hard gates passed; ranked confidence {candidate['confidence_score']}/100 ({candidate['score_band']}); {candidate['context_brief']}."
    candidate["caution"] = "Score is a deterministic screen, not a probability of profit; use current-session NBBO for any later review."
    return candidate


def evaluate_underlying(context: dict[str, Any], chain_payload: Any, config: ScreeningConfig, as_of: date) -> dict[str, Any]:
    valid, base_reasons, floor, floor_source = base_context_gates(context, config)
    result: dict[str, Any] = {"symbol": str(context.get("symbol") or "").upper(), "eligible": [], "rejections": [], "provider_errors": []}
    if not valid or floor is None:
        result["rejections"].append({"scope": "underlying", "reasons": base_reasons})
        return result
    try:
        spot, contracts, chain_date = parse_chain_payload(chain_payload)
    except ScreeningError as exc:
        result["provider_errors"].append(str(exc))
        return result
    # The caller can supply an independently dated official underlying snapshot.
    if spot is None:
        spot = number(context.get("spot")) or number((context.get("jlaw") or {}).get("current_price"))
    if spot is None or spot <= 0:
        result["provider_errors"].append("UNDERLYING_SPOT_MISSING")
        return result
    contract_as_of = chain_date or as_of
    if contract_as_of != as_of:
        result["provider_errors"].append(f"CHAIN_DATE_MISMATCH:{contract_as_of.isoformat()}!=AS_OF:{as_of.isoformat()}")
        return result
    puts = [row for row in contracts if row.get("option_type") == "put"]
    if not puts:
        result["provider_errors"].append("NO_PUT_CONTRACTS")
        return result
    by_expiry: dict[str, list[dict[str, Any]]] = {}
    for put in puts:
        by_expiry.setdefault(str(put.get("expiry")), []).append(put)
    hard_passes: list[dict[str, Any]] = []
    for short_put in puts:
        expiry = as_iso_date(short_put.get("expiry"))
        earnings_ok, earnings_reason, earnings_date = earnings_status(context, expiry or as_of, config.require_earnings_date)
        # CSP has the required strict signed-delta gate.
        passed, reasons, dte = contract_gates(short_put, spot=spot, floor=floor, as_of=as_of,
            min_dte=config.csp_min_dte, max_dte=config.csp_max_dte,
            short_delta_max=config.csp_short_delta_max, config=config)
        if passed and not earnings_ok:
            passed, reasons = False, reasons + [earnings_reason]
        if passed:
            bid, strike = number(short_put.get("bid")), number(short_put.get("strike"))
            collateral = (strike - bid) * config.contract_multiplier if bid is not None and strike is not None else None
            if collateral is None or collateral <= 0:
                result["rejections"].append({"scope": "csp", "contract": short_put.get("option_symbol"), "reasons": ["INVALID_CSP_COLLATERAL"]})
            else:
                candidate = _base_candidate("CSP", context, short_put, dte, floor, floor_source, earnings_date)
                candidate.update({"spot": round(spot, 4), "credit": round(bid * config.contract_multiplier, 2),
                    "collateral": round(collateral, 2), "credit_to_collateral": round((bid * config.contract_multiplier) / collateral, 6),
                    "break_even": round(strike - bid, 4), "reward_metric": round(bid / (strike - bid), 6)})
                hard_passes.append(apply_confidence_score(candidate, context, config))
        elif reasons:
            result["rejections"].append({"scope": "csp", "contract": short_put.get("option_symbol"), "reasons": reasons})
        # BPS deliberately omits all delta gates and delta score inputs.
        passed, reasons, dte = contract_gates(short_put, spot=spot, floor=floor, as_of=as_of,
            min_dte=config.bps_min_dte, max_dte=config.bps_max_dte, short_delta_max=None, config=config)
        if passed and not earnings_ok:
            passed, reasons = False, reasons + [earnings_reason]
        if not passed:
            if reasons:
                result["rejections"].append({"scope": "bps_short", "contract": short_put.get("option_symbol"), "reasons": reasons})
            continue
        strike = number(short_put.get("strike"))
        desired_long = (strike or 0.0) - config.bps_width
        matches = [row for row in by_expiry.get(str(short_put.get("expiry")), []) if number(row.get("strike")) is not None and abs((number(row.get("strike")) or 0.0) - desired_long) < 0.001]
        if not matches:
            result["rejections"].append({"scope": "bps", "contract": short_put.get("option_symbol"), "reasons": ["DEFAULT_WIDTH_LONG_PUT_MISSING"]})
            continue
        for long_put in matches:
            leg_reasons: list[str] = []
            long_oi, long_spread = number(long_put.get("open_interest")), quote_spread_pct(long_put)
            if long_oi is None or long_oi < config.min_open_interest:
                leg_reasons.append("LONG_PUT_OPEN_INTEREST_BELOW_MINIMUM")
            if long_spread is None:
                leg_reasons.append("LONG_PUT_NBBO_QUOTE_MISSING")
            elif long_spread > config.max_bid_ask_spread_pct:
                leg_reasons.append("LONG_PUT_BID_ASK_SPREAD_TOO_WIDE")
            short_bid, long_ask = number(short_put.get("bid")), number(long_put.get("ask"))
            net_credit = short_bid - long_ask if short_bid is not None and long_ask is not None else None
            if net_credit is None or net_credit <= 0:
                leg_reasons.append("NON_POSITIVE_NET_CREDIT")
            if leg_reasons:
                result["rejections"].append({"scope": "bps", "contract": short_put.get("option_symbol"), "reasons": leg_reasons})
                continue
            credit = net_credit * config.contract_multiplier
            max_loss = config.bps_width * config.contract_multiplier - credit
            ror = credit / max_loss if max_loss > 0 else None
            if max_loss <= 0:
                result["rejections"].append({"scope": "bps", "contract": short_put.get("option_symbol"), "reasons": ["INVALID_MAX_LOSS"]})
                continue
            if ror is None or ror < config.bps_min_return_on_risk or ror > config.bps_max_return_on_risk:
                result["rejections"].append({"scope": "bps", "contract": short_put.get("option_symbol"), "reasons": ["BPS_RETURN_ON_RISK_OUTSIDE_20_TO_30_PCT_BAND"]})
                continue
            candidate = _base_candidate("BPS", context, short_put, dte, floor, floor_source, earnings_date)
            candidate.update({"spot": round(spot, 4), "long_put": long_put, "width": round(config.bps_width, 4),
                "credit": round(credit, 2), "max_loss": round(max_loss, 2), "return_on_risk": round(ror, 6),
                "break_even": round((strike or 0.0) - net_credit, 4), "reward_metric": round(ror, 6)})
            hard_passes.append(apply_confidence_score(candidate, context, config))
    result["eligible"] = hard_passes
    return result


def score_band(score: Any) -> str:
    """Return an interpretation label without making score an eligibility filter."""
    value = number(score) or 0.0
    if value >= 85:
        return "Strong screen"
    if value >= 70:
        return "Higher screen"
    if value >= 60:
        return "Moderate screen"
    return "Lower screen"


def rank_candidates(candidates: Iterable[dict[str, Any]], config: ScreeningConfig) -> list[dict[str, Any]]:
    """Rank every hard-pass structure across both strategies; score never excludes one."""
    values = [item for item in candidates if item.get("candidate_status") == "ELIGIBLE"]
    values.sort(key=lambda item: (-int(item.get("confidence_score") or 0), -(number(item.get("reward_metric")) or 0.0), str(item.get("strategy")), str(item.get("symbol")), str(item.get("expiry"))))
    ranked: list[dict[str, Any]] = []
    for rank, item in enumerate(values[:config.rank_limit], start=1):
        item["top_rank"] = rank
        ranked.append(item)
    return ranked


def build_audit(contexts: Iterable[dict[str, Any]], chains: dict[str, Any], config: ScreeningConfig, as_of: date) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    eligible_by_strategy: dict[str, list[dict[str, Any]]] = {"CSP": [], "BPS": []}
    for context in contexts:
        symbol = str(context.get("symbol") or "").upper()
        result = evaluate_underlying(context, chains.get(symbol, {}), config, as_of)
        evaluated.append(result)
        eligible = result.get("eligible", [])
        candidates.extend(eligible)
        for candidate in eligible:
            eligible_by_strategy.setdefault(str(candidate.get("strategy")), []).append(candidate)
    ranked = rank_candidates(candidates, config)
    return {"as_of": as_of.isoformat(), "config": asdict(config), "ranked_top20": ranked, "eligible": eligible_by_strategy, "evaluated": evaluated,
        "counts": {"underlyings_evaluated": len(evaluated), "csp_eligible": len(eligible_by_strategy["CSP"]), "bps_eligible": len(eligible_by_strategy["BPS"]),
                   "ranked_top20": len(ranked), "provider_errors": sum(len(item.get("provider_errors", [])) for item in evaluated)}}


def fmt_money(value: Any) -> str:
    amount = number(value)
    return "—" if amount is None else f"${amount:,.2f}"


def fmt_pct(value: Any) -> str:
    fraction = number(value)
    return "—" if fraction is None else f"{fraction * 100:.2f}%"


def short_contract_label(candidate: dict[str, Any]) -> str:
    short = candidate.get("short_put") or {}
    delta = number(short.get("delta"))
    return f"{short.get('option_symbol') or '$' + str(short.get('strike')) + ' put'} (Δ {delta:.3f})" if delta is not None else str(short.get("option_symbol") or "—")


def bps_contract_label(candidate: dict[str, Any]) -> str:
    short, long = candidate.get("short_put") or {}, candidate.get("long_put") or {}
    return f"{short.get('option_symbol') or '$' + str(short.get('strike'))} / {long.get('option_symbol') or '$' + str(long.get('strike'))}"


def _candidate_lines(items: list[dict[str, Any]], strategy: str) -> list[str]:
    if not items:
        return []
    if strategy == "CSP":
        lines = ["| Score | Symbol | Expiry / DTE | Short put | Credit / collateral | Break-even | Reason |", "|---:|---|---:|---|---:|---:|---|"]
        for item in items:
            lines.append(f"| {item.get('confidence_score', '—')} | {item['symbol']} | {item['expiry']} / {item['dte']} | {short_contract_label(item)} | {fmt_money(item.get('credit'))} / {fmt_money(item.get('collateral'))} ({fmt_pct(item.get('credit_to_collateral'))}) | {fmt_money(item.get('break_even'))} | {item.get('reason', '—').replace('|', '/')} |")
        return lines
    lines = ["| Score | Symbol | Expiry / DTE | Short / long put | Credit / max loss | Return on risk | Break-even | Reason |", "|---:|---|---:|---|---:|---:|---:|---|"]
    for item in items:
        lines.append(f"| {item.get('confidence_score', '—')} | {item['symbol']} | {item['expiry']} / {item['dte']} | {bps_contract_label(item)} | {fmt_money(item.get('credit'))} / {fmt_money(item.get('max_loss'))} | {fmt_pct(item.get('return_on_risk'))} | {fmt_money(item.get('break_even'))} | {item.get('reason', '—').replace('|', '/')} |")
    return lines


def markdown_report(audit: dict[str, Any]) -> str:
    config, counts = audit.get("config") or {}, audit.get("counts") or {}
    ranked = audit.get("ranked_top20") if isinstance(audit.get("ranked_top20"), list) else []
    lines = [f"# CSP & 10-Point Bull Put Spread — Top {config.get('rank_limit', 20)} Ranked Board — {audit.get('as_of', 'unknown date')}", "", "**Research only.** No order instruction or order is created by this workflow.", "", "## Policy",
        f"- Include US-listed requested watchlist names with current J Law **Valid or Watch** status. `Skip` remains excluded; Watch is shown in the board rather than rejected solely for its label.",
        f"- CSP: {config.get('csp_min_dte')}–{config.get('csp_max_dte')} DTE and signed short-put delta strictly < {config.get('csp_short_delta_max')}. BPS: {config.get('bps_min_dte')}–{config.get('bps_max_dte')} DTE, no delta gate, exactly {config.get('bps_width')}-points wide, and {float(config.get('bps_min_return_on_risk', .2))*100:.0f}%–{float(config.get('bps_max_return_on_risk', .3))*100:.0f}% return on risk.",
        f"- All rows below passed structural, safety-floor, earnings, OI, and usable-NBBO gates. **Score is a rank only; it does not filter eligibility.**", "", "## Top ranked hard-pass structures"]
    if not ranked:
        lines.append("No CSP or BPS structure passed every non-score hard gate.")
    else:
        lines.extend(["| Rank | Score / band | Strategy | Symbol / J Law | Expiry / DTE | Structure | Entry economics | Break-even | Key context |", "|---:|---|---|---|---:|---|---:|---:|---|"])
        for item in ranked:
            strategy = str(item.get("strategy") or "—")
            label = short_contract_label(item) if strategy == "CSP" else bps_contract_label(item)
            economics = (f"{fmt_money(item.get('credit'))} / {fmt_money(item.get('collateral'))}" if strategy == "CSP" else f"{fmt_money(item.get('credit'))} / {fmt_money(item.get('max_loss'))} ({fmt_pct(item.get('return_on_risk'))})")
            jlaw = ((item.get("source_context") or {}).get("jlaw") or {}).get("classification", "—")
            context = str(item.get("context_brief") or "—").replace("|", "/")
            lines.append(f"| {item.get('top_rank', '—')} | {item.get('confidence_score', '—')} / {item.get('score_band', '—')} | {strategy} | {item.get('symbol', '—')} / {jlaw} | {item.get('expiry', '—')} / {item.get('dte', '—')} | {label} | {economics} | {fmt_money(item.get('break_even'))} | {context} |")
    exclusions: list[str] = []
    for evaluated in audit.get("evaluated") or []:
        symbol = str(evaluated.get("symbol") or "—") if isinstance(evaluated, dict) else "—"
        reasons = {
            str(reason)
            for item in evaluated.get("rejections", []) if isinstance(item, dict)
            for reason in (item.get("reasons") if isinstance(item.get("reasons"), list) else [])
        } if isinstance(evaluated, dict) else set()
        if "EARNINGS_OVERLAP" in reasons:
            if isinstance(evaluated, dict) and evaluated.get("eligible"):
                exclusions.append(f"- **{symbol}:** longer-dated structures were excluded where earnings occur on or before expiry; earlier eligible expiries remain in the board.")
            else:
                exclusions.append(f"- **{symbol}:** all screened structures were excluded because earnings occur on or before the contract expiry.")
        elif "EARNINGS_UNCONFIRMED" in reasons:
            exclusions.append(f"- **{symbol}:** excluded where applicable because the next earnings date is unconfirmed.")
    if exclusions:
        lines.extend(["", "## Important hard-gate exclusions", *exclusions])
    lines.extend(["", "## Audit", f"- Underlyings evaluated: {counts.get('underlyings_evaluated', 0)}", f"- Hard-pass inventory before Top-{config.get('rank_limit', 20)} truncation: {counts.get('csp_eligible', 0)} CSP; {counts.get('bps_eligible', 0)} BPS.", f"- Rows published: {counts.get('ranked_top20', 0)}. Provider/input exceptions: {counts.get('provider_errors', 0)}.", "- CSP credit uses short-put bid. BPS credit uses short-put bid less long-put ask. Values are per contract and exclude commission, regulatory fees, and slippage.", "- GEX is market-structure context, not a prediction. `sub_resolution` GEX is map-only and does not tighten the safety floor.", "- The confidence score is deterministic and provisional; it is not a probability of profit, assignment probability, or order recommendation.", "", "This is research and analysis only, not personalized financial advice."])
    return "\n".join(lines) + "\n"
