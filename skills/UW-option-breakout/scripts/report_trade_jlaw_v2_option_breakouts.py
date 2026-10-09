#!/usr/bin/env python3
"""Create a review report and optional exact-row option-field patches for trade-jlaw-v2."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

import requests

from trade_jlaw_v2_option_contract import OPTION_COLUMNS, db_patch_payload, validate_update_record

TABLE = "trade-jlaw-v2"


class ReportError(RuntimeError):
    """Raised for a malformed transform artifact or failed requested database patch."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ReportError(f"Missing required environment variable: {name}")
    return value


def rest_base(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/rest/v1") else f"{base}/rest/v1"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_results(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"Could not read transform results: {type(exc).__name__}.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("updates"), list) or not isinstance(payload.get("failures"), list):
        raise ReportError("Transform results must include updates and failures lists.")
    for update in payload["updates"]:
        if not isinstance(update, dict):
            raise ReportError("Transform update records must be JSON objects.")
        validate_update_record(update)
    return payload


def active_rules(update: dict[str, Any]) -> str:
    rules = [f"A{number}" for number in range(1, 9) if update.get(f"option_a{number}")]
    return ", ".join(rules) or "—"


def report_rows(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        updates,
        key=lambda row: (
            -(float(row["option_score"]) if row.get("option_score") is not None else -1),
            not bool(row.get("option_alert")),
            -(float(row["jlaw_score"]) if row.get("jlaw_score") is not None else -1),
            str(row["symbol"]),
        ),
    )
    rows: list[dict[str, Any]] = []
    for rank, update in enumerate(ordered, start=1):
        rows.append(
            {
                "rank": rank,
                "run_date": update["run_date"],
                "symbol": update["symbol"],
                "jlaw_review_type": update["jlaw_review_type"],
                "classification": update.get("classification"),
                "jlaw_score": update.get("jlaw_score"),
                "option_score": update.get("option_score"),
                "option_tier": update.get("option_tier"),
                "option_alert": update.get("option_alert"),
                "active_rules": active_rules(update),
                "option_iv30": update.get("option_iv30"),
                "option_rv20": update.get("option_rv20"),
                "option_iv_rank": update.get("option_iv_rank"),
                "option_put_call_ratio": update.get("option_put_call_ratio"),
                "option_pct_below_high": update.get("option_pct_below_high"),
                "key_signal": update.get("key_signal"),
                "unusual_whales_trade_date": update.get("unusual_whales_trade_date"),
            }
        )
    return rows


def markdown_table(rows: list[dict[str, Any]]) -> str:
    columns = ["rank", "symbol", "jlaw_review_type", "classification", "jlaw_score", "option_score", "option_tier", "option_alert", "active_rules", "key_signal"]
    if not rows:
        return "No transformed option-breakout records."
    header = "| " + " | ".join(columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    data = []
    for row in rows:
        values = [str(row.get(column, "") if row.get(column) is not None else "—").replace("|", "\\|").replace("\n", " ") for column in columns]
        data.append("| " + " | ".join(values) + " |")
    return "\n".join([header, rule, *data])


def build_markdown(results: dict[str, Any], rows: list[dict[str, Any]], apply_requested: bool, patches: list[dict[str, Any]]) -> str:
    failures = results["failures"]
    applied = sum(1 for patch in patches if patch.get("status") == "updated")
    lines = [
        f"# Trade JLaw V2 — Unusual Whales Option Breakout Report — {results.get('run_date', 'Unknown date')}",
        "",
        f"- **Transformed option records:** {len(rows)}",
        f"- **Transformation exceptions:** {len(failures)}",
        f"- **Database patches requested:** {'yes' if apply_requested else 'no'}",
        f"- **Exact-row option patches applied:** {applied}",
        "",
        "## Ranked Option Breakouts",
        markdown_table(rows),
        "",
        "## Provider Notes",
        "- Source: Unusual Whales daily ticker-state snapshots.",
        "- A1 and A6 remain false when the connector does not expose historical 25-delta risk-reversal skew data; no proxy is fabricated.",
        "- Database patches whitelist only the 18 `option_*` fields and match the exact `(id, run_date, symbol, jlaw_review_type)` row.",
    ]
    if failures:
        lines.extend(["", "## Transformation Exceptions", ""])
        lines.extend(f"- `{entry.get('symbol', '[UNKNOWN]')}`: {entry.get('error', 'Unknown error')}" for entry in failures)
    patch_failures = [patch for patch in patches if patch.get("status") != "updated"]
    if patch_failures:
        lines.extend(["", "## Database Patch Exceptions", ""])
        lines.extend(f"- `{entry.get('symbol', '[UNKNOWN]')}`: {entry.get('error', 'Unknown error')}" for entry in patch_failures)
    return "\n".join(lines) + "\n"


def apply_update(update: dict[str, Any]) -> dict[str, Any]:
    """Patch only the option namespace on one immutable enhanced-JLaw row."""
    try:
        url = rest_base(require_env("SUPABASE_URL")) + f'/{TABLE}'
        key = require_env("SUPABASE_KEY")
        params = {
            "id": f"eq.{int(update['id'])}",
            "run_date": f"eq.{update['run_date']}",
            "symbol": f"eq.{update['symbol']}",
            "jlaw_review_type": f"eq.{update['jlaw_review_type']}",
        }
        response = requests.patch(
            url,
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=representation"},
            params=params,
            json=db_patch_payload(update),
            timeout=45,
        )
        if response.status_code not in (200, 204):
            raise ReportError(f"Supabase PATCH failed with HTTP {response.status_code}.")
        returned = response.json() if response.content else []
        if response.status_code == 200 and (not isinstance(returned, list) or len(returned) != 1):
            raise ReportError("Supabase PATCH did not match exactly one trade-jlaw-v2 row.")
        return {"id": update["id"], "symbol": update["symbol"], "jlaw_review_type": update["jlaw_review_type"], "status": "updated"}
    except (requests.RequestException, ReportError, ValueError) as exc:
        return {"id": update.get("id"), "symbol": update.get("symbol"), "jlaw_review_type": update.get("jlaw_review_type"), "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Report and optionally apply trade-jlaw-v2 option-breakout updates.")
    parser.add_argument("--results", required=True, help="option_transform_results.json from the transformation stage.")
    parser.add_argument("--output-dir", required=True, help="Directory for report artifacts.")
    parser.add_argument("--apply", action="store_true", help="Patch only the 18 option_* fields to their exact enhanced-JLaw rows.")
    args = parser.parse_args()
    try:
        results = load_results(Path(args.results))
        rows = report_rows(results["updates"])
        patches = [apply_update(update) for update in results["updates"]] if args.apply else []
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_columns = list(rows[0]) if rows else ["rank", "symbol", "option_score", "option_tier", "option_alert"]
        with (output_dir / "trade_jlaw_v2_option_breakout_report.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=csv_columns)
            writer.writeheader()
            writer.writerows(rows)
        write_json(output_dir / "supabase_option_patch_results.json", patches)
        report = build_markdown(results, rows, args.apply, patches)
        (output_dir / "trade_jlaw_v2_option_breakout_report.md").write_text(report, encoding="utf-8")
        summary = {
            "run_date": results.get("run_date"),
            "reported_records": len(rows),
            "transformation_failures": len(results["failures"]),
            "apply_requested": args.apply,
            "patches_updated": sum(1 for patch in patches if patch.get("status") == "updated"),
            "patches_failed": sum(1 for patch in patches if patch.get("status") == "failed"),
        }
        write_json(output_dir / "report_summary.json", summary)
        print(json.dumps(summary, indent=2))
        return 0 if not args.apply or summary["patches_failed"] == 0 else 1
    except ReportError as exc:
        print(f"REPORT ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
