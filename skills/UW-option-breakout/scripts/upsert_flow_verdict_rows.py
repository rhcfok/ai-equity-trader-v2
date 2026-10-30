"""Unpack flow-verdicts payload blobs into normalized flow_verdict_rows.

Two modes:

1. From a local daily verdict JSON (the SKILL.md output artifact):
   python scripts/upsert_flow_verdict_rows.py --json flow_verdicts_2026-10-16.json \
       [--stats flow_alert_stats_2026-10-16.json]

   With --stats (or a flow_alert_stats_<date>.json next to the verdict JSON) the
   uncapped premium stats from scripts/flow_alert_stats.py are merged into each
   row (alert_count_total, premium_tilt, verdict_quant, verdict_agree, ...).

2. From Supabase itself (backfill / re-sync of existing blob rows):
   python scripts/upsert_flow_verdict_rows.py --pull-from-supabase [--latest N]

Rows are upserted into public.flow_verdict_rows with the
(verdict_date, symbol) primary key; re-running the same date merges instead
of duplicating. The source "flow-verdicts" blob rows are never modified.

Environment: lowercase supabase_url / supabase_service_role (same convention
as scripts/pull_jlaw.py), or SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.
Requires the table created by sql/flow_verdict_rows.sql.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

VERDICT_TABLE = "flow-verdicts"
ROWS_TABLE = "flow_verdict_rows"
UPSERT_PREFER = "resolution=merge-duplicates,on_conflict=verdict_date,symbol"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
    env.setdefault("supabase_url", os.environ.get("SUPABASE_URL", ""))
    env.setdefault("supabase_service_role", os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SERVICE_ROLE", ""))
    return env


def rest_base(raw: str) -> str:
    base = raw.rstrip("/")
    return base if base.endswith("/rest/v1") else base + "/rest/v1"


def headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": UPSERT_PREFER}


def request_with_retry(method: str, url: str, headers_map: dict[str, str], **kwargs) -> requests.Response:
    last: Exception | None = None
    for attempt in range(5):
        try:
            response = requests.request(method, url, headers=headers_map, timeout=100, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            return response
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def rows_from_payload(payload: dict, default_date: str | None = None) -> tuple[str, list[dict]]:
    """Unpack one flow-verdicts payload into normalized rows."""
    date = str(payload.get("date") or default_date or "")
    verdicts = payload.get("verdicts") or []
    market_tide = payload.get("market_tide") or {}
    classifier = payload.get("classifier")
    rows: list[dict] = []
    for item in verdicts:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        rows.append(
            {
                "verdict_date": date,
                "symbol": str(item["symbol"]).upper(),
                "verdict": str(item.get("verdict") or "no-data"),
                "reasons": list(item.get("reasons") or []),
                "caveats": list(item.get("caveats") or []),
                "key_flows": list(item.get("key_flows") or []),
                "alert_count": item.get("alert_count"),
                "earnings_block": item.get("earnings_block"),
                "next_earnings_date": item.get("next_earnings_date"),
                "classifier": classifier,
                "tide_bias": market_tide.get("bias"),
                "tide_date": market_tide.get("date"),
                "tide_note": market_tide.get("note"),
            }
        )
    return date, rows


def merge_stats(rows: list[dict], stats_by_symbol: dict[str, dict]) -> int:
    """Merge flow_alert_stats columns into verdict rows in place; returns rows merged."""
    from flow_alert_stats import STAT_COLUMNS, verdict_agrees  # lazy: avoids a circular import

    merged = 0
    for row in rows:
        stats = stats_by_symbol.get(row["symbol"])
        if not stats:
            continue
        for col in STAT_COLUMNS:
            row[col] = stats.get(col)
        row["verdict_agree"] = verdict_agrees(row.get("verdict"), stats.get("verdict_quant"))
        merged += 1
    return merged


def upsert_rows(base: str, key: str, rows: list[dict]) -> None:
    if not rows:
        print("No rows to upsert.")
        return
    # PostgREST bulk upserts need uniform keys; rows with and without merged stats
    # go in separate batches so missing stats never null out existing ones.
    batches: dict[tuple, list[dict]] = {}
    for row in rows:
        batches.setdefault(tuple(sorted(row)), []).append(row)
    for batch in batches.values():
        response = request_with_retry("POST", f"{base}/{ROWS_TABLE}", headers(key), json=batch)
        if response.status_code not in (200, 201, 204):
            sys.exit(f"Supabase upsert failed (HTTP {response.status_code}): {response.text[:500]}")
    print(f"Upserted {len(rows)} rows into {ROWS_TABLE}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unpack flow-verdicts payloads into flow_verdict_rows.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="Local daily verdict JSON (flow_verdicts_<date>.json).")
    source.add_argument("--pull-from-supabase", action="store_true", help="Read existing flow-verdicts blob rows and unpack them all.")
    parser.add_argument("--stats", help="flow_alert_stats_<date>.json to merge (default: auto-detect next to --json).")
    parser.add_argument("--latest", type=int, default=0, help="With --pull-from-supabase: only the N most recent blob rows (0 = all).")
    args = parser.parse_args()

    env = load_env()
    if not env["supabase_url"] or not env["supabase_service_role"]:
        sys.exit("Missing supabase_url / supabase_service_role (env or .env).")
    base = rest_base(env["supabase_url"])
    key = env["supabase_service_role"]

    total = 0
    if args.json:
        try:
            payload = json.loads(open(args.json, encoding="utf-8").read())
        except (OSError, ValueError) as exc:
            sys.exit(f"Could not read {args.json}: {type(exc).__name__}.")
        date, rows = rows_from_payload(payload)
        print(f"{args.json}: verdict_date={date or '??'} rows={len(rows)}")
        stats_path = args.stats or os.path.join(os.path.dirname(os.path.abspath(args.json)), f"flow_alert_stats_{date}.json")
        if os.path.exists(stats_path):
            stats_doc = json.loads(open(stats_path, encoding="utf-8").read())
            merged = merge_stats(rows, {k.upper(): v for k, v in (stats_doc.get("stats") or {}).items()})
            print(f"Merged flow_alert_stats for {merged}/{len(rows)} rows from {stats_path}")
        elif args.stats:
            sys.exit(f"--stats file not found: {args.stats}")
        upsert_rows(base, key, rows)
        total += len(rows)
    else:
        params = {"select": "*", "order": "verdict_date.desc"}
        if args.latest > 0:
            params["limit"] = str(args.latest)
        response = request_with_retry("GET", f"{base}/{VERDICT_TABLE}", headers(key), params=params)
        if response.status_code != 200:
            sys.exit(f"Could not read {VERDICT_TABLE} (HTTP {response.status_code}): {response.text[:500]}")
        for blob in response.json():
            payload = blob.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    print(f"Skipping {blob.get('verdict_date')}: payload is not JSON.")
                    continue
            date, rows = rows_from_payload(payload, str(blob.get("verdict_date")))
            print(f"{date}: {len(rows)} verdict rows")
            upsert_rows(base, key, rows)
            total += len(rows)
    print(f"Done. Total rows upserted: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
