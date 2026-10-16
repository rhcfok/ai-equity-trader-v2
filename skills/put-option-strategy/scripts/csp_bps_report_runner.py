#!/usr/bin/env python3
"""Render a CSP/BPS opportunity Markdown report from an audit JSON artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from csp_bps_opportunity_core import markdown_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a concise CSP/BPS Markdown report from audit JSON.")
    parser.add_argument("--audit", required=True, help="csp_bps_opportunity_audit.json path")
    parser.add_argument("--output", required=True, help="Markdown report output path")
    args = parser.parse_args()
    try:
        audit = json.loads(Path(args.audit).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"REPORT ERROR: could not load audit JSON: {type(exc).__name__}")
        return 1
    if not isinstance(audit, dict) or not isinstance(audit.get("ranked_top20"), list):
        print("REPORT ERROR: audit JSON does not contain a ranked_top20 array")
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown_report(audit), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
