#!/usr/bin/env python3
"""Prepare Unusual Whales connector fetch requests for trade-jlaw-v2.

The runner reads enhanced-JLaw rows and writes a deterministic fetch manifest.
The calling agent executes each request through the enabled Unusual Whales MCP
connector, then saves the full tool result at the manifest's snapshot path.
No Unusual Whales bearer token is read, accepted, logged, or persisted here.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

TABLE = "trade-jlaw-v2"
TOOL_NAME = "get_ticker_ohlc_latest_or_date"


class FetchError(RuntimeError):
    """Raised for a fatal fetch-manifest condition."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise FetchError(f"Missing required environment variable: {name}")
    return value


def rest_base(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/rest/v1") else f"{base}/rest/v1"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_rows(rows: list[dict[str, Any]], run_date: str, review_type: str | None = None) -> list[dict[str, Any]]:
    """Validate identity records and deterministically deduplicate by table key."""
    keyed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in rows:
        try:
            row_id = int(raw["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchError("trade-jlaw-v2 source row is missing a valid id.") from exc
        row_date = str(raw.get("run_date", ""))
        symbol = str(raw.get("symbol", "")).strip().upper()
        row_review_type = str(raw.get("jlaw_review_type", "")).strip()
        if row_date != run_date or not symbol or not row_review_type:
            raise FetchError("trade-jlaw-v2 source row has an invalid run_date, symbol, or jlaw_review_type.")
        if review_type and row_review_type != review_type:
            continue
        record = {
            "id": row_id,
            "run_date": row_date,
            "symbol": symbol,
            "jlaw_review_type": row_review_type,
            "classification": raw.get("classification"),
            "jlaw_score": raw.get("jlaw_score"),
            "current_price": raw.get("current_price"),
            "regime": raw.get("regime"),
        }
        keyed[(row_date, symbol, row_review_type)] = record
    return sorted(keyed.values(), key=lambda item: (item["symbol"], item["jlaw_review_type"], item["id"]))


def fetch_source_rows(run_date: str, review_type: str | None) -> list[dict[str, Any]]:
    url = rest_base(require_env("SUPABASE_URL")) + f'/{TABLE}'
    key = require_env("SUPABASE_KEY")
    params = {
        "select": "id,run_date,symbol,jlaw_review_type,classification,jlaw_score,current_price,regime",
        "run_date": f"eq.{run_date}",
        "order": "symbol.asc,jlaw_review_type.asc",
        "limit": "5000",
    }
    if review_type:
        params["jlaw_review_type"] = f"eq.{review_type}"
    try:
        response = requests.get(url, headers={"apikey": key, "Authorization": f"Bearer {key}"}, params=params, timeout=45)
    except requests.RequestException as exc:
        raise FetchError(f"Supabase source query failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise FetchError(f"Supabase source query failed with HTTP {response.status_code}.")
    payload = response.json()
    if not isinstance(payload, list):
        raise FetchError("Supabase source query returned an unexpected payload.")
    return payload


def build_manifest(rows: list[dict[str, Any]], run_date: str, snapshots_dir: Path, limit: int) -> dict[str, Any]:
    symbols = sorted({row["symbol"] for row in rows})
    return {
        "contract_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_date": run_date,
        "table": TABLE,
        "source_rows": rows,
        "connector": {
            "server": "unusual-whales",
            "tool": TOOL_NAME,
            "response_storage": "Save each complete MCP tool-result envelope at snapshot_path.",
        },
        "snapshot_requests": [
            {
                "ticker": symbol,
                "arguments": {"ticker": symbol, "date": run_date, "limit": limit},
                "snapshot_path": str((snapshots_dir / f"{symbol}.json").resolve()),
            }
            for symbol in symbols
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Unusual Whales daily-state fetch requests for trade-jlaw-v2.")
    parser.add_argument("--run-date", required=True, help="Enhanced JLaw run date (YYYY-MM-DD).")
    parser.add_argument("--output-dir", required=True, help="Directory for manifest and connector snapshots.")
    parser.add_argument("--review-type", help="Optional exact jlaw_review_type filter.")
    parser.add_argument("--limit", type=int, default=500, choices=range(1, 501), metavar="1..500", help="Daily-state history rows per ticker.")
    parser.add_argument("--input-rows", help="Optional local JSON list for testing; bypasses Supabase read.")
    args = parser.parse_args()
    try:
        datetime.strptime(args.run_date, "%Y-%m-%d")
        output_dir = Path(args.output_dir)
        snapshots_dir = output_dir / "snapshots"
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        raw_rows = json.loads(Path(args.input_rows).read_text(encoding="utf-8")) if args.input_rows else fetch_source_rows(args.run_date, args.review_type)
        if not isinstance(raw_rows, list):
            raise FetchError("--input-rows must contain a JSON list.")
        rows = normalize_rows(raw_rows, args.run_date, args.review_type)
        if not rows:
            raise FetchError("No eligible trade-jlaw-v2 rows found for the requested scope.")
        manifest = build_manifest(rows, args.run_date, snapshots_dir, args.limit)
        write_json(output_dir / "fetch_manifest.json", manifest)
        summary = {
            "run_date": args.run_date,
            "source_rows": len(rows),
            "unique_symbols": len(manifest["snapshot_requests"]),
            "connector_tool": TOOL_NAME,
            "manifest": str((output_dir / "fetch_manifest.json").resolve()),
        }
        write_json(output_dir / "fetch_summary.json", summary)
        print(json.dumps(summary, indent=2))
        return 0
    except FetchError as exc:
        print(f"FETCH ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
