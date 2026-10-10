# Unusual Whales Option Breakout — Trade JLaw V2

A credential-free, three-stage pipeline that enriches `trade-jlaw-v2` rows with deterministic Unusual Whales option-breakout signals.

> This bundle is separate from the ORATS workflow. It does not replace or modify ORATS runners, tables, or schedules.

## Contents

| Path | Purpose |
|---|---|
| `SKILL.md` | End-to-end operational workflow for all three stages. |
| `scripts/fetch_unusual_whales_option_data.py` | Builds a connector request manifest from `trade-jlaw-v2`. |
| `scripts/transform_unusual_whales_option_data.py` | Converts connector-saved daily-state snapshots into validated `option_*` values. |
| `scripts/report_trade_jlaw_v2_option_breakouts.py` | Creates Markdown/CSV reports and can patch only option fields with explicit `--apply`. |
| `scripts/trade_jlaw_v2_option_contract.py` | Whitelisted 18-field update contract. |
| `tests/` | Fixture-based regression tests for the three stages. |

This is the single canonical package location. The repository no longer contains a separate `UW-option-breakout/` root folder or duplicated split packages.

## Execution sequence

1. Run the fetch runner for a `run_date`; it writes `fetch_manifest.json`.
2. For each manifest request, use the configured Unusual Whales connector tool `get_ticker_ohlc_latest_or_date`, then save the full MCP result envelope at the requested snapshot path.
3. Run the transformation runner with the manifest. Review `option_transform_results.json`.
4. Run the report runner to create Markdown and CSV artifacts. Add `--apply` only after review.

## Credential policy

The bundle contains **no API keys, bearer tokens, Supabase URLs, database keys, or environment files**. Runtime credentials are supplied outside source control through the configured connector and environment.

## Current data limitation

The connector provides daily price, IV, IV rank, call/put volume, and rolling volume fields. It does not expose historical 25-delta risk-reversal skew; therefore, A1 and A6 remain false instead of using a fabricated proxy.

## Test

```bash
pip install -r requirements.txt
python3 tests/test_trade_jlaw_v2_unusual_whales_pipeline.py
```
