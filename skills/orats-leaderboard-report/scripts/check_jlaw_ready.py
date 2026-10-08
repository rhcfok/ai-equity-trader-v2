#!/usr/bin/env python3
"""Decide whether today's JLaw screener is complete enough to run Layer 2.

Prints a JSON object to stdout. Exit 0 always unless secrets/HTTP fail.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import requests

WATCHLIST = "watchlist"
JLAW = "n8n-breakout-jlaw"
MIN_COVERAGE = 0.95
STALE_SECONDS = 10 * 60


def rest_base() -> str:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    return url if url.endswith("/rest/v1") else f"{url}/rest/v1"


def headers() -> dict[str, str]:
    key = os.environ["SUPABASE_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Prefer": "count=exact",
    }


def get(path: str, params: dict[str, str]) -> tuple[list, int]:
    r = requests.get(f"{rest_base()}/{path}", headers=headers(), params=params, timeout=45)
    if r.status_code >= 400:
        raise SystemExit(f"Supabase GET {path} failed {r.status_code}: {r.text[:400]}")
    cr = r.headers.get("content-range", "")
    total = int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1].isdigit() else len(r.json())
    data = r.json()
    if not isinstance(data, list):
        raise SystemExit(f"Unexpected payload for {path}")
    return data, total


def main() -> int:
    for name in ("SUPABASE_URL", "SUPABASE_KEY"):
        if not os.environ.get(name):
            raise SystemExit(f"Missing secret {name}")

    run_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).date().isoformat()

    watch_rows, watch_n = get(WATCHLIST, {"select": "symbol", "limit": "1000"})
    watch_symbols = {row["symbol"] for row in watch_rows if row.get("symbol")}

    scored, scored_total = get(
        quote(JLAW, safe=""),
        {
            "select": "symbol,created_at,jlaw_score",
            "date": f"eq.{run_date}",
            "limit": "5000",
        },
    )
    scored_symbols = {row["symbol"] for row in scored if row.get("symbol")}
    last_insert = None
    for row in scored:
        ts = row.get("created_at")
        if ts and (last_insert is None or ts > last_insert):
            last_insert = ts

    stale_ok = False
    age_seconds = None
    if last_insert:
        last_dt = datetime.fromisoformat(last_insert.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
        stale_ok = age_seconds >= STALE_SECONDS

    coverage = (len(scored_symbols) / watch_n) if watch_n else 0.0
    missing = sorted(watch_symbols - scored_symbols)

    ready = watch_n > 0 and coverage >= MIN_COVERAGE and stale_ok

    out = {
        "run_date": run_date,
        "watchlist_n": watch_n,
        "scored_n": len(scored_symbols),
        "row_n": scored_total,
        "coverage": round(coverage, 4),
        "missing_symbols": missing,
        "last_insert_at": last_insert,
        "seconds_since_last_insert": None if age_seconds is None else int(age_seconds),
        "inserts_stale": stale_ok,
        "jlaw_ready": ready,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
