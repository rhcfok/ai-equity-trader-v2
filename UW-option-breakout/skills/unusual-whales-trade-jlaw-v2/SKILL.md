---
name: unusual-whales-trade-jlaw-v2
description: "Run the complete three-stage Unusual Whales option-breakout enrichment pipeline for enhanced JLaw rows in trade-jlaw-v2: connector snapshot acquisition, deterministic transformation, and reviewed reporting or exact-row option updates. Use when operating the end-to-end trade-jlaw-v2 options workflow."
---

# Unusual Whales → Trade JLaw V2 Option Pipeline

This is a new, separate workflow. It must never replace, invoke, alter, or reschedule the existing ORATS pipeline.

## Stages

1. **Fetch:** run `scripts/fetch_unusual_whales_option_data.py` to produce `fetch_manifest.json`. For each request, call the enabled Unusual Whales MCP tool `get_ticker_ohlc_latest_or_date` and save its full result envelope at the supplied snapshot path.
2. **Transform:** run `scripts/transform_unusual_whales_option_data.py` with the manifest. It produces a validated `option_transform_results.json` package and isolated failures.
3. **Report or apply:** run `scripts/report_trade_jlaw_v2_option_breakouts.py` to produce Markdown and CSV reviews. Add `--apply` only to patch the 18 `option_*` fields of exact enhanced-JLaw rows.

Use the stage-specific skills when operating one component in isolation: `unusual-whales-option-fetch`, `unusual-whales-option-transform`, and `trade-jlaw-v2-option-report`.

## Fixed safeguards

- Preserve the unique enhanced-JLaw identity `(id, run_date, symbol, jlaw_review_type)`.
- Never write JLaw fields during option enrichment.
- Never expose bearer credentials in source, output, logs, reports, or email.
- Filter provider history to the requested run date.
- Keep A1 and A6 false until the enabled connector exposes historical risk-reversal skew data.
- Do not modify the existing ORATS pipeline.
