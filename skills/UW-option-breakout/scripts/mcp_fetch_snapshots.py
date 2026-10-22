"""Fetch UW day-state snapshots by driving the official UW MCP server over HTTP.

This is the connector step of PIPELINE.md Stage 1: for each manifest request,
call the MCP tool `get_ticker_ohlc_latest_or_date` and save the full tool-result
envelope at the manifest's snapshot_path. The bearer token is read from the
UW_API_KEY environment variable and never printed or persisted.

Usage: python mcp_fetch_snapshots.py [--manifest fetch_manifest.json] [--only SYM1,SYM2] [--sleep 0.4]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MCP_URL = "https://api.unusualwhales.com/api/mcp"
TOOL = "get_ticker_ohlc_latest_or_date"


def post(body: dict, key: str, session: str | None = None) -> tuple[int, dict | str]:
    request = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("UW-CLIENT-API-ID", "100001")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if session:
        request.add_header("mcp-session-id", session)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
            status = response.status
            session = response.headers.get("mcp-session-id") or session
    except TimeoutError:
        if attempt == tries:
            raise
        print("  retry after TimeoutError", flush=True)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:500]
    lines = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    payload = json.loads(lines[-1]) if lines else json.loads(raw)
    return status, payload


def with_backoff(fn, tries: int = 5):
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            status, payload = fn()
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == tries:
                raise
            print(f"  retry after {type(exc).__name__}", flush=True)
            time.sleep(delay)
            delay *= 2
            continue
        if status == 200 and not (isinstance(payload, dict) and payload.get("error")):
            return payload
        retryable = status in (429, 500, 502, 503, 504) or status == 200
        if not retryable or attempt == tries:
            raise RuntimeError(f"HTTP {status}: {payload if isinstance(payload, str) else json.dumps(payload)[:300]}")
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def envelope_rows(envelope: dict) -> list:
    sc = envelope.get("structuredContent") or {}
    if isinstance(sc.get("result"), list):
        return sc["result"]
    for item in envelope.get("content", []):
        if isinstance(item, dict) and "json" in item.get("type", ""):
            data = json.loads(item.get("text", "null"))
            if isinstance(data, dict) and isinstance(data.get("result"), list):
                return data["result"]
            if isinstance(data, list):
                return data
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="fetch_manifest.json")
    parser.add_argument("--only", help="comma-separated tickers to limit this run")
    parser.add_argument("--sleep", type=float, default=0.4)
    args = parser.parse_args()

    import os
    key = os.environ.get("UW_API_KEY", "").strip()
    if not key:
        print("UW_API_KEY not set", file=sys.stderr)
        return 2

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    session = None

    status, payload = post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "uw-option-breakout-runner", "version": "1.0"}}}, key)
    if status != 200:
        print(f"initialize failed HTTP {status}", file=sys.stderr)
        return 2
    print(f"MCP initialize ok; tools available")

    only = {t.strip().upper() for t in args.only.split(",")} if args.only else None
    todo = [r for r in manifest["snapshot_requests"] if not only or r["ticker"] in only]
    print(f"{len(todo)} snapshot requests to fetch")

    ok = failed = 0
    for index, request in enumerate(todo, start=1):
        ticker = request["ticker"]
        path = Path(request["snapshot_path"])
        if path.exists():
            try:
                if envelope_rows(json.loads(path.read_text(encoding="utf-8"))):
                    print(f"[{index}/{len(todo)}] {ticker}: cached, skip")
                    continue
            except Exception:
                pass
        call_id = 100 + index
        payload = with_backoff(lambda: post({"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                                             "params": {"name": TOOL, "arguments": request["arguments"]}}, key, session))
        result = payload.get("result", {})
        if result.get("isError"):
            print(f"[{index}/{len(todo)}] {ticker}: TOOL ERROR: {json.dumps(result)[:200]}")
            failed += 1
            path.with_suffix(".error.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
            continue
        envelope = dict(result)
        if not envelope_rows(envelope):
            print(f"[{index}/{len(todo)}] {ticker}: no usable rows in result; kept envelope for audit")
            failed += 1
            path.with_suffix(".error.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
            continue
        path.write_text(json.dumps(envelope), encoding="utf-8")
        ok += 1
        rows = envelope_rows(envelope)
        print(f"[{index}/{len(todo)}] {ticker}: {len(rows)} rows saved")
        if index % 10 == 0:
            print(f"  progress: {index}/{len(todo)}", flush=True)
        time.sleep(args.sleep)
    print(f"DONE ok={ok} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
