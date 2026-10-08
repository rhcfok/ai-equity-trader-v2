#!/usr/bin/env python3
"""
Standalone Layer 2: ORATS Breakout Detection, Leaderboards, and Reporting
=========================================================================

Scope: Tasks 3–5 only. The script expects JLaw technical-analysis records to
already exist in Supabase table `n8n-breakout-jlaw`.

It will:
  1. identify Valid / Watch symbols from the latest 14 trading dates;
  2. call ORATS delayed-data endpoints and calculate A1–A8 scores;
  3. refresh the relevant daily rows in `breakout_leaderboard_12` and
     `breakout_leaderboard_3`;
  4. write report artifacts for an AI runner/email node to send.

Requirements:
  pip install requests pandas numpy

Required environment variables:
  SUPABASE_URL   e.g. https://<project>.supabase.co or .../rest/v1
  SUPABASE_KEY   service-role key with read/write REST privileges
  ORATS_TOKEN    ORATS delayed data API token

Optional environment variables:
  RUN_DATE             ISO date; defaults to UTC date. Uses the nearest prior
                       JLaw trading date if RUN_DATE is non-trading.
  OUTPUT_DIR           defaults to ./layer2_output
  EMAIL_WEBHOOK_URL    optional endpoint accepting the generated JSON payload.
  EMAIL_WEBHOOK_TOKEN  optional bearer token for that endpoint.

Email is deliberately a hand-off instead of a provider-specific integration.
The runner writes email_report.txt and email_payload.json, allowing the target
platform's native Gmail/SMTP/email node to send exactly the generated content.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
ORATS_BASE_URL = "https://api.orats.io/datav2"
JLAW_TABLE = "n8n-breakout-jlaw"
LB12_TABLE = "breakout_leaderboard_12"
LB3_TABLE = "breakout_leaderboard_3"
RECIPIENTS = ["r.hcfok@gmail.com", "slimzhu@gmail.com", "zarddaishi33@gmail.com"]
MAX_ORATS_RETRIES = 3
ORATS_BACKOFF_SECONDS = (5, 15, 30)


class PipelineError(RuntimeError):
    """Raised for a non-recoverable pipeline condition."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("YOUR_"):
        raise PipelineError(f"Missing required environment variable: {name}")
    return value


def rest_base(url: str) -> str:
    """Normalize either a Supabase project URL or an existing REST URL."""
    base = url.rstrip("/")
    return base if base.endswith("/rest/v1") else f"{base}/rest/v1"


def safe_number(value: Any, decimals: int = 4) -> float | None:
    """Return finite float values only; JSON cannot represent NaN/Infinity."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, decimals)


def json_safe(value: Any) -> Any:
    """Recursively normalize pandas/numpy values for strict JSON serialization."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(json_safe(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def chunked(items: list[dict[str, Any]], size: int = 250) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ─────────────────────────────────────────────────────────────────────────────
# Supabase: actionable JLaw input and leaderboard refresh
# ─────────────────────────────────────────────────────────────────────────────
class SupabaseRest:
    def __init__(self, url: str, key: str) -> None:
        self.base_url = rest_base(url)
        self.session = requests.Session()
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def get(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/{table}", headers=self.headers, params=params, timeout=45)
        if response.status_code >= 400:
            raise PipelineError(f"Supabase GET {table} failed ({response.status_code}): {response.text[:500]}")
        payload = response.json()
        if not isinstance(payload, list):
            raise PipelineError(f"Supabase GET {table} returned an unexpected payload.")
        return payload

    def delete_for_date(self, table: str, analysis_date: str) -> None:
        """Replace one day only. Needed because current tables only have an id PK."""
        response = self.session.delete(
            f"{self.base_url}/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            params={"Date": f"eq.{analysis_date}"},
            timeout=45,
        )
        if response.status_code >= 400:
            raise PipelineError(f"Supabase DELETE {table} failed ({response.status_code}): {response.text[:500]}")

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        for batch in chunked(rows):
            response = self.session.post(
                f"{self.base_url}/{table}",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=json_safe(batch),
                timeout=60,
            )
            if response.status_code not in (200, 201):
                raise PipelineError(f"Supabase POST {table} failed ({response.status_code}): {response.text[:800]}")

    def refresh_day(self, table: str, analysis_date: str, rows: list[dict[str, Any]], dry_run: bool) -> None:
        """Idempotent per-date replacement with no effect on other historical dates."""
        if dry_run:
            print(f"[DRY RUN] Would replace {len(rows)} row(s) in {table} for {analysis_date}.")
            return
        self.delete_for_date(table, analysis_date)
        if rows:
            self.insert_many(table, rows)
        print(f"[OK] Refreshed {len(rows)} row(s) in {table} for {analysis_date}.")


def interpretation_bucket(value: Any) -> str:
    """Normalize variable historic interpretation text into the downstream buckets."""
    text = str(value or "").strip().lower()
    if "aggressive" in text or "a+" in text or "valid" in text or "actionable" in text:
        return "Valid"
    if "watch" in text:
        return "Watch"
    return "Skip"


def choose_actionable_jlaw_rows(supabase: SupabaseRest, requested_date: str) -> tuple[pd.DataFrame, str, list[str]]:
    """Use the latest 14 available JLaw trading dates ending on/before requested_date."""
    raw = supabase.get(
        JLAW_TABLE,
        {
            "select": "date,symbol,jlaw_score,interpretation,rational,rsi,vcp_stage,pocket_pivot",
            "date": f"lte.{requested_date}",
            "order": "date.desc",
            "limit": "10000",
        },
    )
    if not raw:
        raise PipelineError(f"No JLaw rows found on or before {requested_date}.")

    df = pd.DataFrame(raw)
    required = {"date", "symbol", "jlaw_score", "interpretation"}
    missing = required.difference(df.columns)
    if missing:
        raise PipelineError(f"JLaw rows are missing required field(s): {sorted(missing)}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[df["date"].notna() & df["symbol"].ne("")].copy()
    df["bucket"] = df["interpretation"].apply(interpretation_bucket)

    trading_dates = sorted(df["date"].dt.strftime("%Y-%m-%d").dropna().unique(), reverse=True)[:14]
    if not trading_dates:
        raise PipelineError("No valid JLaw trading dates could be derived.")
    analysis_date = trading_dates[0]
    window = df[df["date"].dt.strftime("%Y-%m-%d").isin(trading_dates)].copy()
    actionable = window[window["bucket"].isin(["Valid", "Watch"])].copy()
    if actionable.empty:
        raise PipelineError(f"No Valid/Watch JLaw signals found across the latest {len(trading_dates)} trading dates.")

    output: list[dict[str, Any]] = []
    for symbol, group in actionable.groupby("symbol", sort=True):
        group = group.sort_values("date")
        valid_count = int((group["bucket"] == "Valid").sum())
        watch_count = int((group["bucket"] == "Watch").sum())
        first_signal_date = group["date"].min().strftime("%Y-%m-%d")
        # Use the most recent actionable state rather than a later non-actionable state.
        latest = group.iloc[-1]
        output.append(
            {
                "symbol": symbol,
                "bucket": latest["bucket"],
                "jlaw_score": safe_number(latest.get("jlaw_score"), 0) or 0.0,
                "valid_count": valid_count,
                "watch_count": watch_count,
                "signal_first_date": first_signal_date,
                "rational": latest.get("rational") or "",
                "rsi": safe_number(latest.get("rsi"), 2),
                "vcp_stage": latest.get("vcp_stage") or "—",
                "pocket_pivot": bool(latest.get("pocket_pivot", False)),
            }
        )

    result = pd.DataFrame(output)
    result["bucket_rank"] = result["bucket"].map({"Valid": 0, "Watch": 1}).fillna(2)
    result = result.sort_values(["bucket_rank", "jlaw_score", "symbol"], ascending=[True, False, True]).drop(columns="bucket_rank")
    return result.reset_index(drop=True), analysis_date, sorted(trading_dates)


# ─────────────────────────────────────────────────────────────────────────────
# ORATS: two-layer breakout detection
# ─────────────────────────────────────────────────────────────────────────────
class OratsClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session = requests.Session()

    def get(self, endpoint: str, ticker: str, fields: str) -> list[dict[str, Any]]:
        params = {"ticker": ticker, "fields": fields, "token": self.token}
        last_error = ""
        for attempt in range(MAX_ORATS_RETRIES):
            try:
                response = self.session.get(f"{ORATS_BASE_URL}/{endpoint}", params=params, timeout=30)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = int(retry_after) if str(retry_after).isdigit() else ORATS_BACKOFF_SECONDS[attempt]
                    print(f"    ORATS rate limited. Retrying {ticker} in {wait}s.")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list) or not data:
                    raise PipelineError(f"ORATS /{endpoint} returned no data for {ticker}.")
                return data
            except (requests.RequestException, ValueError, PipelineError) as exc:
                last_error = str(exc)
                if attempt < MAX_ORATS_RETRIES - 1:
                    wait = ORATS_BACKOFF_SECONDS[attempt]
                    print(f"    ORATS attempt {attempt + 1}/{MAX_ORATS_RETRIES} failed for {ticker}; retrying in {wait}s.")
                    time.sleep(wait)
        raise PipelineError(f"ORATS failed for {ticker}: {last_error}")


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


def analyze_orats(orats: OratsClient, ticker: str) -> dict[str, Any]:
    """Calculate the required 52-week percentile, A1–A8, score, tier, and alert."""
    summary = orats.get(
        "summaries",
        ticker,
        "ticker,tradeDate,stockPrice,iv10d,iv20d,iv30d,dlt25Iv30d,dlt75Iv30d",
    )[0]
    core = orats.get(
        "cores",
        ticker,
        "ticker,tradeDate,cVolu,pVolu,cOi,pOi,clsHvXern20d,clsHvXern60d",
    )[0]
    hist_summary = orats.get(
        "hist/summaries",
        ticker,
        "tradeDate,stockPrice,iv30d,dlt25Iv30d,dlt75Iv30d",
    )
    hist_core = orats.get(
        "hist/cores",
        ticker,
        "tradeDate,cVolu,pVolu,clsHvXern20d",
    )

    trade_date = str(summary["tradeDate"])
    stock_price = float(summary["stockPrice"])
    atm_iv30 = float(summary["iv30d"]) * 100
    put_iv25 = float(summary["dlt25Iv30d"]) * 100
    call_iv25 = float(summary["dlt75Iv30d"]) * 100
    skew = put_iv25 - call_iv25
    rv20 = float(core["clsHvXern20d"])
    call_volume = float(core["cVolu"])
    put_volume = float(core["pVolu"])
    pc_ratio = put_volume / call_volume if call_volume > 0 else float("nan")

    hs = pd.DataFrame(hist_summary)
    hc = pd.DataFrame(hist_core)
    hist = pd.merge(hs, hc, on="tradeDate", how="inner")
    if hist.empty:
        raise PipelineError(f"ORATS historical datasets could not be joined for {ticker}.")
    hist["tradeDate"] = pd.to_datetime(hist["tradeDate"], errors="coerce")
    hist = hist.dropna(subset=["tradeDate"]).sort_values("tradeDate").reset_index(drop=True)
    hist["stock_price"] = pd.to_numeric(hist["stockPrice"], errors="coerce")
    hist["atm_iv30"] = pd.to_numeric(hist["iv30d"], errors="coerce") * 100
    hist["put_iv25"] = pd.to_numeric(hist["dlt25Iv30d"], errors="coerce") * 100
    hist["call_iv25"] = pd.to_numeric(hist["dlt75Iv30d"], errors="coerce") * 100
    hist["skew"] = hist["put_iv25"] - hist["call_iv25"]
    hist["rv20"] = pd.to_numeric(hist["clsHvXern20d"], errors="coerce")
    hist["pc_ratio"] = pd.to_numeric(hist["pVolu"], errors="coerce") / pd.to_numeric(hist["cVolu"], errors="coerce").replace(0, np.nan)
    hist["call_volume"] = pd.to_numeric(hist["cVolu"], errors="coerce")

    current_ts = pd.Timestamp(trade_date)
    prior = hist[hist["tradeDate"] < current_ts].tail(252).copy()
    if prior.empty:
        prior = hist.tail(252).copy()

    skew_rank = percentile_rank(prior["skew"], skew)
    pc_rank = percentile_rank(prior["pc_ratio"], pc_ratio)
    iv_rank = percentile_rank(prior["atm_iv30"], atm_iv30)
    rv_rank = percentile_rank(prior["rv20"], rv20)

    recent_call_volumes = hist["call_volume"].tail(22).iloc[:-1].tail(20).dropna()
    call_vol_avg20 = float(recent_call_volumes.mean()) if not recent_call_volumes.empty else call_volume
    call_vol_ratio = call_volume / call_vol_avg20 if call_vol_avg20 > 0 else 1.0

    last4_iv = hist["atm_iv30"].tail(4).dropna().tolist()
    a5_iv_rising = len(last4_iv) >= 4 and last4_iv[-1] > last4_iv[-2] > last4_iv[-3] > last4_iv[-4]

    last4_skew = hist["skew"].tail(4).dropna().tolist()
    skew_delta_3d = (last4_skew[-1] - last4_skew[-3]) if len(last4_skew) >= 3 else 0.0

    previous_iv = float(hist["atm_iv30"].iloc[-2]) if len(hist) >= 2 and pd.notna(hist["atm_iv30"].iloc[-2]) else atm_iv30
    prior_without_latest = prior.iloc[:-1] if len(prior) > 1 else prior
    previous_iv_rank = percentile_rank(prior_without_latest["atm_iv30"], previous_iv)

    high20_series = hist["stock_price"].tail(21).iloc[:-1].dropna()
    high20 = float(high20_series.max()) if not high20_series.empty else stock_price
    pct_below_high = (high20 - stock_price) / high20 * 100 if high20 > 0 else 0.0

    # Layer 2: Trigger / confirmation signals (4 points maximum)
    a1 = bool(skew_rank is not None and skew_rank > 90.0)           # 2 points
    a2 = bool(math.isfinite(pc_ratio) and pc_ratio < 0.85)          # 1 point
    a3 = bool(rv20 > atm_iv30)                                     # 1 point

    # Layer 1: Setup / leading signals (7 points maximum)
    a4 = bool(call_vol_ratio >= 1.5)                               # 2 points
    a5 = bool(a5_iv_rising)                                        # 1 point
    a6 = bool(skew_delta_3d < 0)                                  # 1 point
    a7 = bool(previous_iv_rank is not None and previous_iv_rank < 30.0 and iv_rank is not None and iv_rank >= 30.0)  # 1
    a8 = bool(pct_below_high <= 2.0)                               # 2 points

    trigger_score = (2 if a1 else 0) + int(a2) + int(a3)
    setup_score = (2 if a4 else 0) + int(a5) + int(a6) + int(a7) + (2 if a8 else 0)
    total_score = trigger_score + setup_score
    alert = total_score >= 5 or ((a1 or a2 or a3) and (a4 or a5 or a6 or a7 or a8))
    tier = signal_tier(total_score)

    active = [name for name, fired in (("A1 skew >90th percentile", a1), ("A2 P/C <0.85", a2), ("A3 RV>IV", a3), ("A4 call-volume spike", a4), ("A5 IV rising", a5), ("A6 skew compression", a6), ("A7 IV-rank cross", a7), ("A8 near 20d high", a8)) if fired]
    key_signal = "; ".join(active[:3]) or "No options conditions fired"

    return {
        "symbol": ticker,
        "orats_trade_date": trade_date,
        "stock_price": safe_number(stock_price, 2),
        "total_score": total_score,
        "max_score": 11,
        "setup_score": setup_score,
        "trigger_score": trigger_score,
        "tier": tier,
        "alert": alert,
        "key_signal": key_signal,
        "a1": a1,
        "a2": a2,
        "a3": a3,
        "a4": a4,
        "a5": a5,
        "a6": a6,
        "a7": a7,
        "a8": a8,
        "iv30": safe_number(atm_iv30),
        "rv20": safe_number(rv20),
        "vrp20": safe_number(atm_iv30 - rv20),
        "iv_rank": safe_number(iv_rank),
        "skew_rank": safe_number(skew_rank),
        "pc_rank": safe_number(pc_rank),
        "rv_rank": safe_number(rv_rank),
        "pc_ratio": safe_number(pc_ratio),
        "call_volume": safe_number(call_volume, 0),
        "put_volume": safe_number(put_volume, 0),
        "call_vol_avg20": safe_number(call_vol_avg20, 0),
        "call_vol_ratio": safe_number(call_vol_ratio),
        "skew": safe_number(skew),
        "skew_delta_3d": safe_number(skew_delta_3d),
        "iv_trend": "RISING" if a5 else "STABLE",
        "pct_below_high": safe_number(pct_below_high),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Leaderboards and report generation
# ─────────────────────────────────────────────────────────────────────────────
def conviction_tier(score: float) -> str:
    if score > 8.5:
        return "HIGH"
    if score > 6.0:
        return "MEDIUM"
    if score > 3.0:
        return "LOW"
    return "MINIMAL"


def make_leaderboards(combined: pd.DataFrame, analysis_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], pd.DataFrame]:
    if combined.empty:
        return [], [], combined
    combined = combined.copy()
    combined["conviction_score"] = (combined["jlaw_score"].astype(float) / 16.0 * 5.0) + (combined["total_score"].astype(float) / 11.0 * 5.0)
    combined["conviction_score"] = combined["conviction_score"].round(2)
    combined["conviction_tier"] = combined["conviction_score"].apply(conviction_tier)
    combined = combined.sort_values(["conviction_score", "jlaw_score", "total_score", "symbol"], ascending=[False, False, False, True]).reset_index(drop=True)
    combined["rank"] = combined.index + 1

    lb12: list[dict[str, Any]] = []
    lb3: list[dict[str, Any]] = []
    for _, row in combined.iterrows():
        lb12.append(
            {
                "Symbol": row["symbol"],
                "JLaw": int(row["jlaw_score"]),
                "A#/Valid#": int(row["valid_count"]),
                "Watch#": int(row["watch_count"]),
                "Signal start date": row["signal_first_date"],
                # Exact target type is PostgreSQL real: numeric only, never "x/11".
                "Options score": float(row["total_score"]),
                "Tier": row["tier"],
                "Alert": bool(row["alert"]),
                "A1": bool(row["a1"]), "A2": bool(row["a2"]), "A3": bool(row["a3"]), "A4": bool(row["a4"]),
                "A5": bool(row["a5"]), "A6": bool(row["a6"]), "A7": bool(row["a7"]), "A8": bool(row["a8"]),
                "IV30": safe_number(row["iv30"]),
                "RV20": safe_number(row["rv20"]),
                "IV Rank": safe_number(row["iv_rank"]),
                "Skew Rank": safe_number(row["skew_rank"]),
                "P/C": safe_number(row["pc_ratio"]),
                "IV↑↓": row["iv_trend"],
                "%Below High": safe_number(row["pct_below_high"]),
                "Date": analysis_date,
            }
        )
        lb3.append(
            {
                # The present Supabase LB3 schema has no Rank column. Rank is retained
                # in local report/CSV output and can be added once the schema changes.
                "Symbol": row["symbol"],
                "Bucket": row["bucket"],
                "JLaw": str(int(row["jlaw_score"])),
                "Options": f"{int(row['total_score'])}/11",
                "Conviction": f"{float(row['conviction_score']):.2f} {row['conviction_tier']}",
                "Key Signal": row["key_signal"],
                "Action": "EXTEND LONG" if row["bucket"] == "Valid" else "WATCH",
                "Date": analysis_date,
            }
        )
    return lb12, lb3, combined


def dataframe_markdown(df: pd.DataFrame, columns: list[str], max_rows: int | None = None) -> str:
    view = df[columns].copy()
    if max_rows is not None:
        view = view.head(max_rows)
    if view.empty:
        return "No qualifying records."
    return view.to_markdown(index=False)


def build_report(combined: pd.DataFrame, analysis_date: str, attempted: int, failures: list[dict[str, str]]) -> str:
    valid = combined[combined["bucket"] == "Valid"].copy()
    watch = combined[combined["bucket"] == "Watch"].copy()
    top = combined.head(15).copy()
    report = [
        f"# Daily Options Breakout Report — {analysis_date}",
        "",
        f"The run attempted ORATS analysis for **{attempted}** actionable symbols and completed **{len(combined)}**. "
        f"It produced **{len(valid)} Valid** and **{len(watch)} Watch** leaderboard rows.",
        "",
        "## Leaderboard 3rd — Top Combined Conviction Ranking",
        dataframe_markdown(top, ["rank", "symbol", "bucket", "jlaw_score", "total_score", "conviction_score", "conviction_tier", "key_signal", "Action" if "Action" in top.columns else "bucket"]),
        "",
        "## Leaderboard 1st — Extend Long (A+ / Valid)",
        dataframe_markdown(valid, ["symbol", "jlaw_score", "valid_count", "watch_count", "signal_first_date", "total_score", "tier", "alert", "vcp_stage", "pocket_pivot", "iv30", "rv20", "iv_rank", "skew_rank", "pc_ratio", "pct_below_high"], max_rows=25),
        "",
        "## Leaderboard 2nd — Long (Watch)",
        dataframe_markdown(watch, ["symbol", "jlaw_score", "valid_count", "watch_count", "total_score", "tier", "alert", "vcp_stage", "pocket_pivot", "iv30", "rv20", "iv_rank", "skew_rank", "pc_ratio", "pct_below_high"], max_rows=25),
        "",
        "## Signal Attribute Legend",
        "| Attribute | Test | Points | Interpretation |",
        "|:--|:--|--:|:--|",
        "| A1 | Skew rank > 90th percentile | 2 | Extreme put-skew ranking; contrarian bullish trigger. |",
        "| A2 | Put/call volume ratio < 0.85 | 1 | Call-side volume dominance. |",
        "| A3 | RV20 > ATM IV30 | 1 | Realized movement exceeds implied volatility. |",
        "| A4 | Call volume / 20-day average ≥ 1.5× | 2 | Unusual call-volume expansion. |",
        "| A5 | ATM IV rises for 3 consecutive sessions | 1 | IV expansion into a potential move. |",
        "| A6 | 3-day skew change < 0 | 1 | Put premium is compressing. |",
        "| A7 | IV rank crosses from <30% to ≥30% | 1 | Volatility lifts from a low percentile. |",
        "| A8 | Price within 2% of prior 20-day high | 2 | Price is coiled near breakout resistance. |",
        "",
        "**Scoring:** Trigger layer (A1–A3) = 4 points maximum. Setup layer (A4–A8) = 7 points maximum. Total options score = 11 points maximum. Tiers: STRONG ≥8, MODERATE 5–7, DEVELOPING 3–4, WEAK <3.",
        "",
        "**Conviction formula:** `(JLaw / 16 × 5) + (Options / 11 × 5)`; maximum = 10.00.",
    ]
    if failures:
        report += ["", "## Symbols Not Processed", ""]
        report += [f"- `{entry['symbol']}`: {entry['error']}" for entry in failures]
    return "\n".join(report) + "\n"


def dispatch_optional_webhook(payload: dict[str, Any], dry_run: bool) -> None:
    url = os.environ.get("EMAIL_WEBHOOK_URL", "").strip()
    if not url:
        return
    if dry_run:
        print("[DRY RUN] Would POST email_payload.json to EMAIL_WEBHOOK_URL.")
        return
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("EMAIL_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.post(url, headers=headers, json=json_safe(payload), timeout=45)
    if response.status_code >= 400:
        raise PipelineError(f"Email webhook failed ({response.status_code}): {response.text[:500]}")
    print("[OK] Handed report to configured email webhook.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run standalone Layer 2 ORATS breakout detection and leaderboard reporting.")
    parser.add_argument("--run-date", default=os.environ.get("RUN_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d")), help="Requested JLaw date (YYYY-MM-DD).")
    parser.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", "layer2_output"), help="Directory for CSV/JSON/email artifacts.")
    parser.add_argument("--dry-run", action="store_true", help="Analyze and write local files, but do not modify Supabase or call email webhook.")
    args = parser.parse_args()

    try:
        datetime.strptime(args.run_date, "%Y-%m-%d")
    except ValueError:
        print("ERROR: --run-date must be YYYY-MM-DD.", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        supabase = SupabaseRest(require_env("SUPABASE_URL"), require_env("SUPABASE_KEY"))
        orats = OratsClient(require_env("ORATS_TOKEN"))
        jlaw, analysis_date, trading_dates = choose_actionable_jlaw_rows(supabase, args.run_date)
        print(f"[INFO] Analysis date: {analysis_date}; window: {len(trading_dates)} trading date(s); actionable: {len(jlaw)}.")

        successful: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        # Checkpoint/resume for dry runs: survives shell timeouts. Scoring unchanged.
        ckpt_path = output_dir / ".checkpoint.json"
        done_tickers: set[str] = set()
        if ckpt_path.exists():
            try:
                ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
                if ckpt.get("run_date") == args.run_date:
                    successful = ckpt.get("successful", [])
                    failures = ckpt.get("failures", [])
                    done_tickers = {str(r.get("symbol")) for r in successful} | {str(f.get("symbol")) for f in failures}
                    print(f"[INFO] Resumed checkpoint: {len(done_tickers)} ticker(s) already processed.")
            except Exception:
                pass
        jlaw_symbols = [str(s) for s in jlaw["symbol"].tolist()]

        def save_ckpt() -> None:
            ckpt_path.write_text(json.dumps(json_safe({
                "run_date": args.run_date,
                "successful": successful,
                "failures": failures,
            }), ensure_ascii=False), encoding="utf-8")

        for position, (_, jlaw_row) in enumerate(jlaw.iterrows(), start=1):
            ticker = str(jlaw_row["symbol"])
            if ticker in done_tickers:
                continue
            print(f"[{position}/{len(jlaw)}] ORATS: {ticker}", flush=True)
            try:
                result = analyze_orats(orats, ticker)
                result.update(jlaw_row.to_dict())
                successful.append(result)
                # Gentle pacing reduces 429 responses on lower ORATS plans.
                time.sleep(0.35)
            except Exception as exc:  # Keep the daily batch moving after a single symbol failure.
                error = str(exc)
                print(f"    [SKIP] {ticker}: {error}", flush=True)
                failures.append({"symbol": ticker, "error": error})
            save_ckpt()
        # Ensure checkpoint actually covers the full current actionable set.
        if set(jlaw_symbols) - done_tickers - {str(r.get("symbol")) for r in successful} - {str(f.get("symbol")) for f in failures}:
            raise PipelineError("Checkpoint mismatch; rerun to complete remaining symbols.")

        if not successful:
            raise PipelineError("ORATS did not return a usable result for any actionable symbol; nothing was written.")

        combined = pd.DataFrame(successful)
        lb12_rows, lb3_rows, ranked = make_leaderboards(combined, analysis_date)
        ranked["Action"] = np.where(ranked["bucket"].eq("Valid"), "EXTEND LONG", "WATCH")

        # Local audit artifacts are always written, including during a dry run.
        jlaw.to_csv(output_dir / "actionable_jlaw_signals.csv", index=False)
        ranked.to_csv(output_dir / "combined_ranked_results.csv", index=False)
        pd.DataFrame(lb12_rows).to_csv(output_dir / "leaderboard_12.csv", index=False)
        pd.DataFrame(lb3_rows).assign(Rank=ranked["rank"].tolist()).to_csv(output_dir / "leaderboard_3.csv", index=False)
        write_json(output_dir / "orats_results.json", successful)
        write_json(output_dir / "orats_failures.json", failures)

        # Current tables have only `id` as a PK. Replace the selected Date's rows
        # to make reruns idempotent; do not rely on merge-duplicates without a
        # unique (Date, Symbol) constraint.
        supabase.refresh_day(LB12_TABLE, analysis_date, lb12_rows, args.dry_run)
        supabase.refresh_day(LB3_TABLE, analysis_date, lb3_rows, args.dry_run)

        report = build_report(ranked, analysis_date, attempted=len(jlaw), failures=failures)
        (output_dir / "email_report.txt").write_text(report, encoding="utf-8")
        email_payload = {
            "to": RECIPIENTS,
            "subject": f"Daily Options Breakout Report — {analysis_date}",
            "body": report,
            "content_type": "text/markdown",
            "attachments": [str(output_dir / "leaderboard_12.csv"), str(output_dir / "leaderboard_3.csv")],
        }
        write_json(output_dir / "email_payload.json", email_payload)
        dispatch_optional_webhook(email_payload, args.dry_run)

        summary = {
            "analysis_date": analysis_date,
            "trading_dates_used": trading_dates,
            "actionable_symbols": len(jlaw),
            "orats_successes": len(successful),
            "orats_failures": len(failures),
            "leaderboard_12_rows": len(lb12_rows),
            "leaderboard_3_rows": len(lb3_rows),
            "output_dir": str(output_dir.resolve()),
            "dry_run": args.dry_run,
        }
        write_json(output_dir / "run_summary.json", summary)
        print(json.dumps(summary, indent=2))
        print(f"[OK] Send {output_dir / 'email_payload.json'} through the platform's email node.")
        return 0
    except PipelineError as exc:
        print(f"PIPELINE ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
