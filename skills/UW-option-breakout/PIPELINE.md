# Trade JLaw V2 — Unusual Whales Option Breakout Pipeline

## Scope

This is a **new, parallel** three-stage workflow that enriches `trade-jlaw-v2` with Unusual Whales option-breakout fields. It does not replace, invoke, modify, or schedule the existing ORATS pipeline.

The enhanced-JLaw row identity is fixed as:

```text
(id, run_date, symbol, jlaw_review_type)
```

Option enrichment never changes the enhanced JLaw methodology fields: `classification`, `jlaw_score`, `current_price`, `regime`, or `jlaw_review_type`.

## Stage 1 — Fetch

**Runner:** `fetch_unusual_whales_option_data.py`
**Skill:** `unusual-whales-option-fetch`

The fetch runner reads selected `trade-jlaw-v2` rows and writes `fetch_manifest.json`. It does not obtain or store an Unusual Whales API key.

For each manifest request, an agent calls the enabled Unusual Whales MCP tool `get_ticker_ohlc_latest_or_date` with:

```json
{"ticker":"TICKER","date":"YYYY-MM-DD","limit":500}
```

Save the full connector result envelope at the manifest-provided snapshot path. This produces one private daily-state history per symbol.

## Stage 2 — Transform

**Runner:** `transform_unusual_whales_option_data.py`
**Skill:** `unusual-whales-option-transform`

The transformer reads the manifest and connector snapshots, filters rows to the requested run date, applies the deterministic option-breakout rules, and writes a review package. It creates one option update per enhanced-JLaw row, even when multiple review types share a symbol snapshot.

| `trade-jlaw-v2` column | Source/result field |
|---|---|
| `option_score`, `option_tier`, `option_alert` | Total breakout score, tier, and alert |
| `option_a1`–`option_a8` | Fixed option-breakout conditions |
| `option_iv30`, `option_rv20` | 30-day implied and 20-day realized volatility |
| `option_iv_rank`, `option_skew_rank` | Volatility and skew percentile metrics |
| `option_put_call_ratio` | Daily put/call volume ratio |
| `option_iv_trend` | IV direction classification |
| `option_pct_below_high` | Distance from prior 20-day high |

Current connector limitation: historical 25-delta risk-reversal skew is not exposed, so **A1 and A6 remain false**. The pipeline does not invent a proxy.

## Stage 3 — Report and Optional Apply

**Runner:** `report_trade_jlaw_v2_option_breakouts.py`
**Skill:** `trade-jlaw-v2-option-report`

By default the report runner generates a ranked Markdown report, CSV export, and patch audit JSON. It does not modify Supabase.

After review, run with `--apply` to patch only the 18 `option_*` fields. Every patch includes all four identity predicates and must update exactly one row; a mismatch is a failure.

## Artifacts

```text
<output>/fetch_manifest.json
<output>/fetch_summary.json
<output>/snapshots/<TICKER>.json
<output>/transform/option_transform_results.json
<output>/transform/option_transform_failures.json
<output>/report/trade_jlaw_v2_option_breakout_report.md
<output>/report/trade_jlaw_v2_option_breakout_report.csv
<output>/report/supabase_option_patch_results.json
<output>/report/report_summary.json
```

## Safety constraints

- Use connector-backed snapshots; never expose or write a bearer token.
- Preserve existing ORATS workflows and schedules.
- Continue after isolated provider data errors, recording them in transformation failures.
- Do not apply any update when the transformation output contains zero usable rows.
- Verify report and patch audit artifacts before stating that a run completed.
