---
name: uw-option-breakout
description: "Run the complete three-stage Unusual Whales option-breakout enrichment pipeline for enhanced JLaw rows in trade-jlaw-v2: connector snapshot acquisition, deterministic transformation, and reviewed reporting or exact-row option updates. Use when operating the end-to-end trade-jlaw-v2 options workflow."
---

# Unusual Whales Option Breakout — Trade JLaw V2

This self-contained skill replaces the prior duplicated root bundle and split stage packages. It is separate from the ORATS pipeline and must never invoke, alter, or reschedule ORATS workflows.

## Stage 1 — Fetch

Run `scripts/fetch_unusual_whales_option_data.py` to build `fetch_manifest.json`. For each request, call the enabled Unusual Whales connector tool `get_ticker_ohlc_latest_or_date` and save its complete MCP result envelope at the manifest’s `snapshot_path`.

## Stage 2 — Transform

Run `scripts/transform_unusual_whales_option_data.py` with the fetch manifest. It converts connector snapshots into one validated option update per enhanced-JLaw row and writes `option_transform_results.json` plus isolated failures.

## Stage 3 — Report or apply

Run `scripts/report_trade_jlaw_v2_option_breakouts.py` to produce Markdown and CSV reviews. Add `--apply` only after review; it patches only the 18 `option_*` fields on exactly matched enhanced-JLaw rows.

## Fixed safeguards

- Preserve the unique enhanced-JLaw identity `(id, run_date, symbol, jlaw_review_type)`.
- Never write JLaw methodology fields during option enrichment.
- Never expose bearer credentials in source, output, logs, reports, or email.
- Filter provider history to the requested run date.
- Keep A1 and A6 false until the enabled connector exposes historical risk-reversal skew data.
- Do not modify the existing ORATS pipeline.
