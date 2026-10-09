"""Unusual Whales data adapter for the Layer 2 breakout leaderboard.

This module intentionally preserves the existing A1-A8 rules and returns the
same output keys consumed by the leaderboard/reporting layer.  It only replaces
the market-data acquisition layer.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class UnusualWhalesError(RuntimeError):
    """Raised when Unusual Whales cannot provide a usable, non-secret response."""


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _numeric_column(frame: pd.DataFrame, column: str, multiplier: float = 1.0) -> pd.Series:
    return pd.to_numeric(frame.get(column), errors="coerce") * multiplier


def percentile_rank(series: pd.Series, value: float) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty or not math.isfinite(value):
        return None
    return round(float((clean < value).sum() / len(clean) * 100), 4)


def signal_tier(score: int) -> str:
    if score >= 8:
        return "STRONG"
    if score >= 5:
        return "MODERATE"
    if score >= 3:
        return "DEVELOPING"
    return "WEAK"


class UnusualWhalesSnapshotClient:
    """Read API Basic ticker-day-state snapshots fetched through the connector.

    Each `<TICKER>.json` file must be either a raw list of daily state rows or
    the MCP tool-result envelope containing `structuredContent.result`. This
    keeps bearer credentials out of the runner, logs, artifacts, and filesystem.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        self.snapshot_dir = Path(snapshot_dir)

    def get_ticker_day_state(self, ticker: str, end_date: str | None = None) -> list[dict[str, Any]]:
        path = self.snapshot_dir / f"{ticker.upper().strip()}.json"
        if not path.exists():
            raise UnusualWhalesError(f"Unusual Whales snapshot is missing for {ticker.upper().strip()}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnusualWhalesError(f"Unusual Whales snapshot could not be read for {ticker.upper().strip()}: {type(exc).__name__}.") from exc
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("structuredContent", {}).get("result")
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            raise UnusualWhalesError(f"Unusual Whales snapshot has no usable ticker-day rows for {ticker.upper().strip()}.")
        return rows


def _date_frame(rows: list[dict[str, Any]], source: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "date" not in frame.columns:
        raise UnusualWhalesError(f"Unusual Whales {source} rows have no date field.")
    frame["date_ts"] = pd.to_datetime(frame["date"], errors="coerce", utc=True).dt.tz_localize(None)
    frame = frame.dropna(subset=["date_ts"]).sort_values("date_ts").drop_duplicates("date_ts", keep="last")
    if frame.empty:
        raise UnusualWhalesError(f"Unusual Whales {source} has no usable dated rows.")
    return frame.reset_index(drop=True)


def analyze_unusual_whales(client: Any, ticker: str, end_date: str | None = None) -> dict[str, Any]:
    """Calculate the existing 52-week percentile, A1-A8, score, tier and alert.

    The API Basic-compatible ticker-day-state endpoint supplies all daily inputs:
    30-day implied volatility, one-year IV rank, daily close, call/put option
    volume, and rolling 30-day call volume.  The unchanged scoring contract uses
    those inputs directly, with a deterministic 20-session close return proxy
    for realized volatility and a neutral unavailable-skew treatment.  A1 and
    A6 therefore remain false until a provider entitlement exposes historical
    25-delta risk reversal data; no signal is fabricated.
    """
    encoded = ticker.upper().strip()
    day_rows = client.get_ticker_day_state(encoded, end_date)
    history = _date_frame(day_rows, "ticker daily state")
    if end_date:
        cutoff = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(cutoff):
            raise UnusualWhalesError("Requested analysis date is invalid.")
        history = history[history["date_ts"] <= cutoff].copy()
        if history.empty:
            raise UnusualWhalesError(f"Unusual Whales returned no ticker-day rows on or before {end_date} for {encoded}.")
    history["stock_price"] = _numeric_column(history, "close")
    history["atm_iv30"] = _numeric_column(history, "volatility_30", 100.0)
    history["call_volume"] = _numeric_column(history, "call_volume")
    history["put_volume"] = _numeric_column(history, "put_volume")
    history["call_vol_avg20"] = _numeric_column(history, "avg_30_day_call_volume")
    history["iv_rank_api"] = _numeric_column(history, "iv_rank")
    history["skew"] = np.nan
    history = history.dropna(subset=["stock_price", "atm_iv30"]).copy()
    if len(history) < 22:
        raise UnusualWhalesError(f"Unusual Whales returned fewer than 22 usable daily rows for {encoded}.")

    log_returns = np.log(history["stock_price"] / history["stock_price"].shift(1))
    history["rv20"] = log_returns.rolling(20).std() * math.sqrt(252) * 100.0
    trade_date = history["date_ts"].max()
    current_row = history.iloc[-1]
    stock_price = _finite_float(current_row.get("stock_price"))
    if stock_price is None or stock_price <= 0:
        raise UnusualWhalesError(f"Unusual Whales has no usable stock price for {encoded}.")

    atm_iv30 = _finite_float(current_row.get("atm_iv30"))
    rv20 = _finite_float(current_row.get("rv20"))
    if atm_iv30 is None or rv20 is None:
        raise UnusualWhalesError(f"Unusual Whales does not yet have a complete IV/RV window for {encoded}.")
    call_volume = _finite_float(current_row.get("call_volume"))
    put_volume = _finite_float(current_row.get("put_volume"))
    if call_volume is None or put_volume is None:
        raise UnusualWhalesError(f"Unusual Whales daily option-volume data is incomplete for {encoded}.")
    pc_ratio = put_volume / call_volume if call_volume > 0 else float("nan")

    prior = history[history["date_ts"] < trade_date].tail(252).copy()
    if prior.empty:
        prior = history.tail(252).copy()
    skew_rank = None
    pc_rank = percentile_rank(prior["put_volume"] / prior["call_volume"].replace(0, np.nan), pc_ratio)
    iv_rank = _finite_float(current_row.get("iv_rank_api"))
    if iv_rank is None:
        iv_rank = percentile_rank(prior["atm_iv30"], atm_iv30)
    rv_rank = percentile_rank(prior["rv20"], rv20)

    call_vol_avg20 = _finite_float(current_row.get("call_vol_avg20"))
    if call_vol_avg20 is None or call_vol_avg20 <= 0:
        recent_call_volumes = history["call_volume"].tail(22).iloc[:-1].tail(20).dropna()
        call_vol_avg20 = float(recent_call_volumes.mean()) if not recent_call_volumes.empty else call_volume
    call_vol_ratio = call_volume / call_vol_avg20 if call_vol_avg20 > 0 else 1.0

    last4_iv = history["atm_iv30"].tail(4).dropna().tolist()
    a5_iv_rising = len(last4_iv) >= 4 and last4_iv[-1] > last4_iv[-2] > last4_iv[-3] > last4_iv[-4]

    skew_value = None
    skew_delta_3d = None

    prior_iv = history[history["date_ts"] < trade_date]["atm_iv30"].dropna()
    previous_iv = float(prior_iv.iloc[-1]) if not prior_iv.empty else atm_iv30
    prior_without_latest = prior.iloc[:-1] if len(prior) > 1 else prior
    previous_iv_rank = percentile_rank(prior_without_latest["atm_iv30"], previous_iv)

    high20_series = history[history["date_ts"] < trade_date]["stock_price"].tail(20).dropna()
    high20 = float(high20_series.max()) if not high20_series.empty else stock_price
    pct_below_high = (high20 - stock_price) / high20 * 100 if high20 > 0 else 0.0

    # Fixed scoring contract: do not change these A1-A8 tests or point weights.
    a1 = bool(skew_rank is not None and skew_rank > 90.0)             # 2 points
    a2 = bool(math.isfinite(pc_ratio) and pc_ratio < 0.85)            # 1 point
    a3 = bool(rv20 > atm_iv30)                                       # 1 point
    a4 = bool(call_vol_ratio >= 1.5)                                 # 2 points
    a5 = bool(a5_iv_rising)                                          # 1 point
    a6 = bool(skew_delta_3d is not None and skew_delta_3d < 0)       # 1 point
    a7 = bool(previous_iv_rank is not None and previous_iv_rank < 30.0 and iv_rank is not None and iv_rank >= 30.0)  # 1 point
    a8 = bool(pct_below_high <= 2.0)                                 # 2 points

    trigger_score = (2 if a1 else 0) + int(a2) + int(a3)
    setup_score = (2 if a4 else 0) + int(a5) + int(a6) + int(a7) + (2 if a8 else 0)
    total_score = trigger_score + setup_score
    alert = total_score >= 5 or ((a1 or a2 or a3) and (a4 or a5 or a6 or a7 or a8))
    tier = signal_tier(total_score)
    active = [
        name
        for name, fired in (
            ("A1 skew >90th percentile", a1),
            ("A2 P/C <0.85", a2),
            ("A3 RV>IV", a3),
            ("A4 call-volume spike", a4),
            ("A5 IV rising", a5),
            ("A6 skew compression", a6),
            ("A7 IV-rank cross", a7),
            ("A8 near 20d high", a8),
        )
        if fired
    ]

    def safe(value: Any, decimals: int = 4) -> float | None:
        number = _finite_float(value)
        return round(number, decimals) if number is not None else None

    return {
        "symbol": encoded,
        "unusual_whales_trade_date": trade_date.strftime("%Y-%m-%d"),
        "stock_price": safe(stock_price, 2),
        "total_score": total_score,
        "max_score": 11,
        "setup_score": setup_score,
        "trigger_score": trigger_score,
        "tier": tier,
        "alert": alert,
        "key_signal": "; ".join(active[:3]) or "No options conditions fired",
        "a1": a1,
        "a2": a2,
        "a3": a3,
        "a4": a4,
        "a5": a5,
        "a6": a6,
        "a7": a7,
        "a8": a8,
        "iv30": safe(atm_iv30),
        "rv20": safe(rv20),
        "vrp20": safe(atm_iv30 - rv20),
        "iv_rank": safe(iv_rank),
        "skew_rank": safe(skew_rank),
        "pc_rank": safe(pc_rank),
        "rv_rank": safe(rv_rank),
        "pc_ratio": safe(pc_ratio),
        "call_volume": safe(call_volume, 0),
        "put_volume": safe(put_volume, 0),
        "call_vol_avg20": safe(call_vol_avg20, 0),
        "call_vol_ratio": safe(call_vol_ratio),
        "skew": safe(skew_value),
        "skew_delta_3d": safe(skew_delta_3d),
        "iv_trend": "RISING" if a5 else "STABLE",
        "pct_below_high": safe(pct_below_high),
        "data_provider": "Unusual Whales",
        "skew_expiry": None,
    }
