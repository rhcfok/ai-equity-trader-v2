#!/usr/bin/env python3
"""Transform Unusual Whales snapshots into trade-jlaw-v2 option-breakout updates.

This stage is deterministic and never writes Supabase. It consumes the fetch
manifest plus connector-saved snapshots and emits a reviewable update package.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_jlaw_v2_option_contract import OPTION_COLUMNS, option_fields_from_analysis, validate_update_record
from unusual_whales_layer2_provider import UnusualWhalesSnapshotClient, analyze_unusual_whales


class TransformError(RuntimeError):
    """Raised for malformed handoff artifacts."""


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransformError(f"Could not read fetch manifest: {type(exc).__name__}.") from exc
    if not isinstance(payload, dict):
        raise TransformError("Fetch manifest must be a JSON object.")
    required = {"run_date", "source_rows", "snapshot_requests"}
    missing = required.difference(payload)
    if missing or not isinstance(payload["source_rows"], list) or not isinstance(payload["snapshot_requests"], list):
        raise TransformError(f"Fetch manifest is missing required structure: {sorted(missing)}.")
    try:
        datetime.strptime(str(payload["run_date"]), "%Y-%m-%d")
    except ValueError as exc:
        raise TransformError("Fetch manifest run_date must be YYYY-MM-DD.") from exc
    return payload


def snapshot_directory(manifest: dict[str, Any]) -> Path:
    requests = manifest["snapshot_requests"]
    if not requests:
        raise TransformError("Fetch manifest has no snapshot requests.")
    paths = [Path(str(request.get("snapshot_path", ""))) for request in requests]
    if any(not path.name.endswith(".json") for path in paths):
        raise TransformError("Fetch manifest contains an invalid snapshot path.")
    parents = {path.parent.resolve() for path in paths}
    if len(parents) != 1:
        raise TransformError("Fetch manifest must place all snapshots in one directory.")
    return parents.pop()


def transform_manifest(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Generate one immutable-keyed option update per enhanced-JLaw row."""
    run_date = str(manifest["run_date"])
    snapshot_client = UnusualWhalesSnapshotClient(snapshot_directory(manifest))
    updates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source in manifest["source_rows"]:
        symbol = str(source.get("symbol", "")).strip().upper()
        try:
            if not symbol:
                raise TransformError("Source row missing symbol.")
            analysis = analyze_unusual_whales(snapshot_client, symbol, end_date=run_date)
            update = {
                "id": source.get("id"),
                "run_date": str(source.get("run_date", "")),
                "symbol": symbol,
                "jlaw_review_type": source.get("jlaw_review_type"),
                "classification": source.get("classification"),
                "jlaw_score": source.get("jlaw_score"),
                "current_price": source.get("current_price"),
                "regime": source.get("regime"),
                "unusual_whales_trade_date": analysis["unusual_whales_trade_date"],
                "data_provider": analysis["data_provider"],
                "key_signal": analysis["key_signal"],
                **option_fields_from_analysis(analysis),
            }
            validate_update_record(update)
            updates.append(update)
        except Exception as exc:
            failures.append({"symbol": symbol or "[UNKNOWN]", "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    updates.sort(key=lambda row: (row["symbol"], str(row["jlaw_review_type"]), int(row["id"])))
    return updates, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Transform Unusual Whales snapshots into trade-jlaw-v2 option updates.")
    parser.add_argument("--manifest", required=True, help="fetch_manifest.json from the fetch stage.")
    parser.add_argument("--output-dir", required=True, help="Directory for reviewable transformation artifacts.")
    args = parser.parse_args()
    try:
        manifest = load_manifest(Path(args.manifest))
        updates, failures = transform_manifest(manifest)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        result = {
            "contract_version": 1,
            "run_date": manifest["run_date"],
            "source_manifest": str(Path(args.manifest).resolve()),
            "table": "trade-jlaw-v2",
            "option_columns": list(OPTION_COLUMNS),
            "updates": updates,
            "failures": failures,
        }
        write_json(output_dir / "option_transform_results.json", result)
        write_json(output_dir / "option_transform_failures.json", failures)
        summary = {
            "run_date": manifest["run_date"],
            "source_rows": len(manifest["source_rows"]),
            "transformed_updates": len(updates),
            "transform_failures": len(failures),
            "result_path": str((output_dir / "option_transform_results.json").resolve()),
        }
        write_json(output_dir / "option_transform_summary.json", summary)
        print(json.dumps(summary, indent=2))
        return 0 if updates else 1
    except TransformError as exc:
        print(f"TRANSFORM ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
