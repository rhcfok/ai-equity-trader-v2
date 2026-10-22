# Unusual Whales Option Breakout — Trade JLaw V2

A credential-free, three-stage pipeline that enriches `trade-jlaw-v2` rows with deterministic Unusual Whales option-breakout signals.

> This bundle is separate from the ORATS workflow. It does not replace or modify ORATS runners, tables, or schedules.

## Contents

| Path | Purpose |
|---|---|
| `SKILL.md` | End-to-end operational workflow for all three stages. |
| `scripts/fetch_unusual_whales_option_data.py` | Builds a connector request manifest from `trade-jlaw-v2`. |
| `scripts/mcp_fetch_snapshots.py` | Hardened connector driver: calls the official UW MCP server over HTTP (`/api/mcp`, tool `get_ticker_ohlc_latest_or_date`) per manifest request and saves result envelopes; retries 429/timeouts/transport errors (TimeoutError, SSL URLError), resumes from cached snapshots. |
| `scripts/transform_unusual_whales_option_data.py` | Converts connector-saved daily-state snapshots into validated `option_*` values. |
| `scripts/report_trade_jlaw_v2_option_breakouts.py` | Creates Markdown/CSV reports and can patch only option fields with explicit `--apply`. |
| `scripts/trade_jlaw_v2_option_contract.py` | Whitelisted 18-field update contract. |
| `scripts/upsert_flow_verdict_rows.py` | Unpacks daily `flow-verdicts` payloads into the normalized per-symbol `flow_verdict_rows` table (also backfills existing blob rows with `--pull-from-supabase`). |
| `sql/flow_verdict_rows.sql` | DDL for the normalized per-symbol verdict table (run once in the Supabase SQL editor). |
| `sql/option_opportunity.sql` | DDL for the daily opportunity ledger `public.option_opportunity` (run once in the Supabase SQL editor). |
| `references/option_opportunity_contract.md` | Data contract: row identity, lifecycle/outcome states, win-rate formula, economics conventions. |
| `tests/` | Fixture-based regression tests for the three stages. |

This is the single canonical package location. The repository no longer contains a separate `UW-option-breakout/` root folder or duplicated split packages.

## Execution sequence

1. Run the fetch runner for a `run_date`; it writes `fetch_manifest.json`.
2. For each manifest request, use the configured Unusual Whales connector tool `get_ticker_ohlc_latest_or_date`, then save the full MCP result envelope at the requested snapshot path.
3. Run the transformation runner with the manifest. Review `option_transform_results.json`.
4. Run the report runner to create Markdown and CSV artifacts, then apply. The daily flow uses `--apply --max-failure-ratio 0.02`; the runner skips all patches (and exits non-zero) when the transformation-failure share exceeds the guard. Manual review runs may omit `--apply` and re-run later.

## Credential policy

The bundle contains **no API keys, bearer tokens, Supabase URLs, database keys, or environment files**. Runtime credentials are supplied outside source control through the configured connector and environment.

## Current data limitation

The connector provides daily price, IV, IV rank, call/put volume, and rolling volume fields. It does not expose historical 25-delta risk-reversal skew; therefore, A1 and A6 remain false instead of using a fabricated proxy.

## Test

```bash
pip install -r requirements.txt
python3 tests/test_trade_jlaw_v2_unusual_whales_pipeline.py
```
