---
name: unusual-whales-option-fetch
description: Prepare and execute connector-backed Unusual Whales daily option-data fetches for enhanced JLaw rows in trade-jlaw-v2. Use when option snapshots are needed before transforming or reporting the trade-jlaw-v2 option-breakout fields.
---

# Unusual Whales Option Fetch for Trade JLaw V2

Keep this pipeline separate from every ORATS workflow. Read `trade-jlaw-v2`; do not change its JLaw fields or any ORATS table, skill, source file, or schedule.

## Build the fetch manifest

Use the runner to select enhanced-JLaw rows for one date and create a private snapshots directory:

```bash
python3 /home/ubuntu/skills/unusual-whales-option-fetch/scripts/fetch_unusual_whales_option_data.py \
  --run-date YYYY-MM-DD \
  --review-type yahoo \
  --output-dir /home/ubuntu/trade_jlaw_v2_option_output/YYYY-MM-DD
```

`--review-type` is optional. The manifest preserves the exact `(id, run_date, symbol, jlaw_review_type)` identity and deduplicates connector calls by symbol.

## Fetch connector snapshots

Read `fetch_manifest.json`. For each `snapshot_requests` item, call the enabled connector tool:

```json
{"ticker":"TICKER","date":"YYYY-MM-DD","limit":500}
```

Save the **complete MCP tool-result JSON envelope** at the item’s `snapshot_path`. The transform runner reads `structuredContent.result` from that file.

Do not pass, print, store, or infer an Unusual Whales bearer token. Do not create synthetic option data. Isolated connector errors may be documented separately; leave their snapshot absent so the transform stage records the symbol as a failure.

## Handoff

Pass `fetch_manifest.json` to `unusual-whales-option-transform`. Confirm that every expected snapshot file is either present or explicitly documented as a provider failure before moving on.
