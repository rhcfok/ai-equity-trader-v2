#!/usr/bin/env python3
"""Premium-weighted, uncapped flow-alert statistics per ticker.

Why: Step 2 used to call ``get_flow_alerts(..., limit=20)`` and the verdict was a
judgement over the latest 20 alerts. On 2026-10-29, 41 of the 70 bullish names
had exactly 20 alerts (the cap), and a 3-alert name got the same label as a
20-alert name. This script pages through *all* alerts in the window
(``limit=200`` per page, ``older_than`` cursor, up to ``--max-pages``) and
aggregates them by premium and side so the verdict has a quantitative anchor.

Side attribution (per alert, from ``total_ask_side_prem`` / ``total_bid_side_prem``):

    bullish premium = call ask-side + put bid-side
    bearish premium = put ask-side  + call bid-side

each weighted by tenor: DTE <= 42 -> 1.0, 43-365 -> 0.6, > 365 -> 0.25 (LEAPS
are mostly hedges / overwrites). Long-dated (> 365 DTE) ask-side put premium is
also tracked separately as ``protective_premium``.

``premium_tilt`` = (bull - bear) / (bull + bear) in [-1, 1].

``verdict_quant`` (deterministic, used as an anchor and cross-check, it does not
replace the agent verdict):

    no-data     total premium < $100K or < 3 alerts
    protective  tilt < 0 and protective_premium >= 50% of bearish premium
    bullish     tilt >= +0.25 and bullish premium >= $250K
    bearish     tilt <= -0.25 and bearish premium >= $250K
    mixed       otherwise

Usage:

    # Before judging verdicts (Step 2): stats for the day's candidates
    python scripts/flow_alert_stats.py --date 2026-10-29 --symbols AAPL,MU,... --out .

    # Candidates = the symbols already in flow_verdict_rows for that date, and
    # patch the stats columns onto those rows (backfill / after upsert)
    python scripts/flow_alert_stats.py --date 2026-10-29 --from-verdict-rows --patch-verdict-rows

Environment: UW_API_KEY (or UNUSUAL_WHALES_API_KEY); supabase_url /
supabase_service_role (or SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) for
Supabase access. Requires the stats columns in sql/flow_verdict_rows.sql.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upsert_flow_verdict_rows import ROWS_TABLE, load_env, request_with_retry, rest_base  # noqa: E402

UW_BASE_URL = "https://api.unusualwhales.com"
UW_CLIENT_API_ID = "100001"
PAGE_SIZE = 200  # documented API maximum
DEFAULT_MAX_PAGES = 5
NEAR_DTE = 42
LEAPS_DTE = 365
TENOR_WEIGHT = {"near": 1.0, "mid": 0.6, "leaps": 0.25}
MIN_TOTAL_PREMIUM = 100_000.0
MIN_ALERTS = 3
TILT_DIRECTIONAL = 0.25
MIN_SIDE_PREMIUM = 250_000.0

STAT_COLUMNS = (
    "alert_count_total",
    "alerts_capped",
    "call_premium",
    "put_premium",
    "bull_premium",
    "bear_premium",
    "protective_premium",
    "premium_tilt",
    "sweep_share",
    "near_term_share",
    "largest_alert",
    "verdict_quant",
    "verdict_agree",
    "flow_stats_at",
)

DIRECTION_CLASS = {
    "bullish": "bull",
    "bearish": "bear",
    "protective": "bear",
    "mixed": "neutral",
    "no-data": "neutral",
    "covered-call-selling": "neutral",
}


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _dte(expiry: Any, as_of: date) -> int | None:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(str(expiry)[:10]) - as_of).days
    except ValueError:
        return None


def tenor_bucket(dte: int | None) -> str:
    if dte is None or dte <= NEAR_DTE:
        return "near"
    if dte <= LEAPS_DTE:
        return "mid"
    return "leaps"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_flow_alerts(
    session: requests.Session,
    api_key: str,
    ticker: str,
    newer_than: datetime,
    max_pages: int = DEFAULT_MAX_PAGES,
    base_url: str = UW_BASE_URL,
    pause_s: float = 0.6,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Page through /api/option-trades/flow-alerts newest-first.

    Returns (alerts, capped, pages). ``capped`` is True when the last page was
    full and max_pages stopped the walk, i.e. there may be older alerts left.
    """
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "UW-CLIENT-API-ID": UW_CLIENT_API_ID,
    }
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()
    older_than: str | None = None
    pages = 0
    capped = False
    while pages < max_pages:
        params = {"ticker_symbol": ticker, "newer_than": newer_than.isoformat(), "limit": str(PAGE_SIZE)}
        if older_than:
            params["older_than"] = older_than
        response = None
        for attempt in range(4):
            try:
                response = session.get(f"{base_url}/api/option-trades/flow-alerts", headers=headers, params=params, timeout=45)
            except (requests.RequestException, TimeoutError):
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            break
        if response is None or not response.ok:
            code = None if response is None else response.status_code
            raise RuntimeError(f"flow-alerts {ticker} failed (HTTP {code})")
        payload = response.json()
        page = payload.get("data") if isinstance(payload, dict) else payload
        page = [row for row in (page or []) if isinstance(row, dict)]
        pages += 1
        fresh = 0
        for row in page:
            key = str(row.get("id") or (row.get("option_chain"), row.get("created_at"), row.get("total_premium")))
            if key in seen:
                continue
            seen.add(key)
            alerts.append(row)
            fresh += 1
        if len(page) < PAGE_SIZE or fresh == 0:
            break
        stamps = [ts for ts in (_parse_ts(row.get("created_at")) for row in page) if ts]
        if not stamps:
            break
        older_than = min(stamps).isoformat()
        if pages >= max_pages:
            capped = True
        time.sleep(pause_s)
    return alerts, capped, pages


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def classify_quant(stats: dict[str, Any]) -> str:
    total = stats["call_premium"] + stats["put_premium"]
    if stats["alert_count_total"] < MIN_ALERTS or total < MIN_TOTAL_PREMIUM:
        return "no-data"
    tilt = stats["premium_tilt"]
    bull, bear = stats["bull_premium"], stats["bear_premium"]
    if tilt is not None and tilt < 0 and bear > 0 and stats["protective_premium"] >= 0.5 * bear:
        return "protective"
    if tilt is not None and tilt >= TILT_DIRECTIONAL and bull >= MIN_SIDE_PREMIUM:
        return "bullish"
    if tilt is not None and tilt <= -TILT_DIRECTIONAL and bear >= MIN_SIDE_PREMIUM:
        return "bearish"
    return "mixed"


def aggregate_alerts(alerts: Iterable[dict[str, Any]], as_of: date, capped: bool = False) -> dict[str, Any]:
    rows = list(alerts)
    call_prem = put_prem = bull = bear = protective = sweep_prem = near_prem = 0.0
    largest: dict[str, Any] | None = None
    for row in rows:
        kind = str(row.get("type") or "").lower()
        prem = _num(row.get("total_premium"))
        ask = _num(row.get("total_ask_side_prem"))
        bid = _num(row.get("total_bid_side_prem"))
        dte = _dte(row.get("expiry"), as_of)
        bucket = tenor_bucket(dte)
        w = TENOR_WEIGHT[bucket]
        if kind == "call":
            call_prem += prem
            bull += w * ask
            bear += w * bid
        elif kind == "put":
            put_prem += prem
            bull += w * bid
            bear += w * ask
            if bucket == "leaps":
                protective += w * ask
        else:
            continue
        if row.get("has_sweep") in (True, "true", "t", 1):
            sweep_prem += prem
        if bucket == "near":
            near_prem += prem
        if largest is None or prem > largest["premium"]:
            side = "ask" if ask > bid else ("bid" if bid > ask else "mid")
            largest = {
                "type": kind,
                "strike": row.get("strike"),
                "expiry": str(row.get("expiry") or "")[:10] or None,
                "premium": round(prem, 0),
                "side": side,
                "rule": row.get("alert_rule") or row.get("rule_name"),
                "sweep": bool(row.get("has_sweep")),
            }
    total = call_prem + put_prem
    directional = bull + bear
    stats: dict[str, Any] = {
        "alert_count_total": len(rows),
        "alerts_capped": bool(capped),
        "call_premium": round(call_prem, 0),
        "put_premium": round(put_prem, 0),
        "bull_premium": round(bull, 0),
        "bear_premium": round(bear, 0),
        "protective_premium": round(protective, 0),
        "premium_tilt": round((bull - bear) / directional, 4) if directional > 0 else None,
        "sweep_share": round(sweep_prem / total, 4) if total > 0 else None,
        "near_term_share": round(near_prem / total, 4) if total > 0 else None,
        "largest_alert": largest,
    }
    stats["verdict_quant"] = classify_quant(stats)
    return stats


def verdict_agrees(agent_verdict: str | None, quant_verdict: str | None) -> bool | None:
    if not agent_verdict or not quant_verdict:
        return None
    a = DIRECTION_CLASS.get(str(agent_verdict).strip().lower())
    q = DIRECTION_CLASS.get(str(quant_verdict).strip().lower())
    if a is None or q is None:
        return None
    return a == q


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------


def _sb_headers(key: str, prefer: str | None = None) -> dict[str, str]:
    out = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        out["Prefer"] = prefer
    return out


def verdict_rows_for_date(base: str, key: str, verdict_date: str) -> dict[str, str]:
    response = request_with_retry(
        "GET",
        f"{base}/{ROWS_TABLE}",
        _sb_headers(key),
        params={"select": "symbol,verdict", "verdict_date": f"eq.{verdict_date}", "limit": "2000"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Could not read {ROWS_TABLE} (HTTP {response.status_code}): {response.text[:300]}")
    return {str(r["symbol"]).upper(): str(r.get("verdict") or "") for r in response.json()}


def patch_verdict_row(base: str, key: str, verdict_date: str, symbol: str, stats: dict[str, Any]) -> bool:
    body = {col: stats.get(col) for col in STAT_COLUMNS}
    response = request_with_retry(
        "PATCH",
        f"{base}/{ROWS_TABLE}",
        _sb_headers(key, "return=minimal"),
        params={"verdict_date": f"eq.{verdict_date}", "symbol": f"eq.{symbol}"},
        json=body,
    )
    return response.status_code in (200, 204)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Uncapped, premium-weighted flow-alert stats per ticker.")
    parser.add_argument("--date", required=True, help="verdict_date (YYYY-MM-DD)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--symbols", help="Comma-separated tickers")
    src.add_argument("--from-verdict-rows", action="store_true", help="Use the symbols already in flow_verdict_rows for --date")
    parser.add_argument("--window-days", type=int, default=5, help="Calendar-day lookback (matches the verdict window)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Pages of 200 alerts per ticker")
    parser.add_argument("--out", default=".", help="Directory for flow_alert_stats_<date>.json")
    parser.add_argument("--patch-verdict-rows", action="store_true", help="PATCH stats columns onto existing flow_verdict_rows")
    args = parser.parse_args()

    api_key = os.environ.get("UW_API_KEY") or os.environ.get("UNUSUAL_WHALES_API_KEY") or ""
    if not api_key:
        sys.exit("Missing UW_API_KEY / UNUSUAL_WHALES_API_KEY.")
    as_of = date.fromisoformat(args.date)

    env = load_env()
    base = rest_base(env["supabase_url"]) if env.get("supabase_url") else ""
    key = env.get("supabase_service_role", "")
    need_sb = args.from_verdict_rows or args.patch_verdict_rows
    if need_sb and (not base or not key):
        sys.exit("Missing supabase_url / supabase_service_role (env or .env).")

    agent_verdicts: dict[str, str] = verdict_rows_for_date(base, key, args.date) if need_sb else {}
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(agent_verdicts)
    if not symbols:
        sys.exit("No symbols to process.")

    newer_than = datetime.combine(as_of - timedelta(days=args.window_days), datetime.min.time(), tzinfo=timezone.utc)
    now_iso = datetime.now(timezone.utc).isoformat()
    session = requests.Session()
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    patched = 0
    for i, sym in enumerate(symbols, 1):
        try:
            alerts, capped, pages = fetch_flow_alerts(session, api_key, sym, newer_than, args.max_pages)
        except Exception as exc:  # keep going; record per-symbol failure
            errors[sym] = str(exc)[:200]
            continue
        stats = aggregate_alerts(alerts, as_of, capped)
        stats["pages"] = pages
        stats["flow_stats_at"] = now_iso
        stats["verdict_agree"] = verdict_agrees(agent_verdicts.get(sym), stats["verdict_quant"])
        results[sym] = stats
        if args.patch_verdict_rows and sym in agent_verdicts:
            patched += 1 if patch_verdict_row(base, key, args.date, sym, stats) else 0
        print(
            f"[{i}/{len(symbols)}] {sym}: alerts={stats['alert_count_total']}{'+' if capped else ''} "
            f"tilt={stats['premium_tilt']} quant={stats['verdict_quant']} agent={agent_verdicts.get(sym, '-')}"
        )
        time.sleep(0.8)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"flow_alert_stats_{args.date}.json"
    out_path.write_text(
        json.dumps({"date": args.date, "window_days": args.window_days, "stats": results, "errors": errors}, indent=2),
        encoding="utf-8",
    )
    disagree = [s for s, v in results.items() if v.get("verdict_agree") is False]
    print(f"Wrote {out_path} ({len(results)} symbols, {len(errors)} errors, {patched} rows patched)")
    if disagree:
        print("Agent vs quant verdict disagreements: " + ", ".join(disagree))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
