---
name: unusual-whales-option-transform
description: Transform connector-saved Unusual Whales daily ticker-state snapshots into validated option-breakout updates for trade-jlaw-v2. Use after the Unusual Whales option fetch manifest and snapshots exist, before generating a report or applying option fields.
---

# Unusual Whales Option Transformation for Trade JLaw V2

Run this stage only for the separate enhanced-JLaw pipeline. It never writes Supabase and does not touch ORATS workflows.

## Input contract

Require `fetch_manifest.json` from `unusual-whales-option-fetch` and one saved MCP result envelope per available symbol. The snapshots must be in the manifest’s private `snapshots/` directory, named `<TICKER>.json`.

## Transform

```bash
python3 /home/ubuntu/skills/unusual-whales-option-transform/scripts/transform_unusual_whales_option_data.py \
  --manifest /home/ubuntu/trade_jlaw_v2_option_output/YYYY-MM-DD/fetch_manifest.json \
  --output-dir /home/ubuntu/trade_jlaw_v2_option_output/YYYY-MM-DD/transform
```

The runner calculates the fixed A1–A8 logic from daily close, IV, IV rank, call/put volume, and rolling call volume. It filters provider rows after the requested run date. Since the current connector lacks historical 25-delta risk-reversal skew, A1 and A6 remain false; do not substitute a proxy.

The output validates and maps exactly these nullable `trade-jlaw-v2` fields:

`option_score`, `option_tier`, `option_alert`, `option_a1`–`option_a8`, `option_iv30`, `option_rv20`, `option_iv_rank`, `option_skew_rank`, `option_put_call_ratio`, `option_iv_trend`, and `option_pct_below_high`.

## Handoff and exceptions

Inspect `option_transform_results.json`, `option_transform_summary.json`, and `option_transform_failures.json`. Continue after isolated missing or malformed snapshots, but do not apply or report a run with zero transformed records. Hand the result file to `trade-jlaw-v2-option-report`.
