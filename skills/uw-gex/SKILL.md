---
name: uw-gex-workflow
description: Fetch Unusual Whales option-contract data, compute unsigned GEX snapshots, and insert rows into Supabase gex_snapshot. Use as the Unusual Whales alternative to the existing ORATS GEX workflow.
---

# UW GEX Workflow

Compute gamma-exposure (GEX) snapshots for optionable underlyings and insert them into Supabase `gex_snapshot`. This feeds `gex-briefing`.

## Required secrets

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `UW_API_KEY` (or `UNUSUAL_WHALES_API_KEY`)

The API key is sent only in `Authorization: Bearer` calls to `https://api.unusualwhales.com`; do not log it. Requests also require `UW-CLIENT-API-ID` (default `100001`).

## Optional configuration

| Variable | Default |
|---|---|
| `SNAPSHOT_TABLE` | `gex_snapshot` |
| `SUPABASE_WATCHLIST_TABLE` | `watchlist` |
| `UW_WATCHLIST_CATEGORIES` | `core,satellite,watch1` |
| `UW_MAX_WATCHLIST_SYMBOLS` | `200` |
| `BRIEFING_SYMBOLS` | empty; optional explicit symbol override for a one-off run |
| `UW_BASE` | `https://api.unusualwhales.com` |
| `UW_DTE_FILTER` | `0,30` |
| `UW_CLIENT_API_ID` | `100001` |
| `BATCH_SIZE` | `1` |
| `REQUEST_TIMEOUT_SEC` | `45` |
| `RUN_LABEL` | `manual` |
| `GEX_MIN_OI` | `50` |
| `GEX_MONEYNESS_PCT` | `20` |
| `GEX_RESOLUTION_MM` | `1.0` |
| `DRY_RUN` | `false` |

## Procedure

1. Confirm the three required secrets are bound. The default universe is read from `public.watchlist`, with categories `core`, `satellite`, and `watch1` in that order.
2. Optionally run with `DRY_RUN=true` to validate source access and GEX math without inserting snapshots.
3. Run:

```bash
python3 /home/user/skills/uw-gex-workflow/scripts/uw-gex_workflow_runner.py
```

4. Verify the final JSON object. A full run ends with `"failed_count": 0`; partial runs list failures and insert only successfully computed symbols.
5. Generate `gex-briefing` only after reading the completed-run result.

The runner selects only explicitly US-listed watchlist rows (NASDAQ, NYSE, AMEX/ARCA/BATS/CBOE/IEX, or `US`/`USA`), deduplicates symbols in category order, and logs the selected universe. `BRIEFING_SYMBOLS` is a deliberate one-off override; it does not change the default source of truth.

## Source and calculation

The runner first reads `id`, `symbol`, `exchange`, and `category` from the configured Supabase watchlist. For each accepted symbol, it uses documented Unusual Whales REST endpoints:

1. `/api/stock/{ticker}/greek-exposure/expiry` selects expiries in the configured DTE range. Its provider-calculated GEX fields are **not used**.
2. `/api/stock/{ticker}/quote` provides the underlying spot.
3. `/api/stock/{ticker}/option-contracts` is paginated for every selected expiry and supplies contract gamma and open interest. The runner requests another page whenever the previous page contains 500 rows.
4. Calls and puts are grouped by `(expiry, strike)`, filtered, and the nearest-DTE expiry is retained per strike.
5. Per-side magnitude: `gamma × oi × 100 × spot × 0.01 / 1e6` ($MM for a 1% move).
6. Call wall is the largest call-side magnitude at or above spot; put wall is the largest put-side magnitude at or below spot. Net GEX is call-side minus put-side magnitude.

> **SPX coverage:** SPX is excluded from the default UW universe. Unusual Whales documents CBOE index spot prices as unavailable without separately provisioned CBOE access; the UW contract response does not include a usable underlying spot. Add SPX only after that entitlement is confirmed.

## Hard rule — unsigned only

Option open interest is signed-neutral. This workflow **does not** infer dealer long/short gamma and must not use Unusual Whales signed/assumed dealer-GEX fields as a substitute. `zero_gamma` remains null, `signed_source` is `none`, and briefing output must call all call/put measures unsigned magnitudes.

## Snapshot schema

Inserted columns remain unchanged: `symbol`, `snapshot_ts`, `spot_price`, `net_gex`, `net_gex_mm`, `abs_gex_mm`, `zero_gamma`, `peak_gex_strike`, `call_wall`, `call_wall_gex_mm`, `put_wall`, `put_wall_gex_mm`, `regime`, `near_term_gex_pct`, `strike_count`, `raw_rows`, `pos_gex_above_spot_mm`, `pos_gex_below_spot_mm`, `gravity`, and `gamma_ladder`.

Rows are append-only; downstream readers use the latest `snapshot_ts` per symbol. `trade_date`, `validity`, expiry list, and `signed_source` are local log-only fields because the existing table does not expose those columns.

## Failure handling

| Symptom | Action |
|---|---|
| Unusual Whales 401/403 | Check or rotate the API key and plan entitlement; do not retry automatically. |
| Unusual Whales 429 | The runner backs off twice, then records the symbol failure. |
| Transient timeout or incomplete response | The runner retries twice with bounded backoff, then records the symbol failure. |
| Empty categorized watchlist | Confirm the `watchlist` rows, category names, and US exchange values before running. |
| Missing CBOE-index quote | Confirm provider CBOE index-price provisioning; do not substitute an unrelated source. |
| Supabase 400 column error | Review the schema listed above. |
| Supabase 401/403 | Correct `SUPABASE_SERVICE_ROLE_KEY`. |
| `failed_count > 0` | Report failed symbols; do not invent snapshots or issue a briefing as if all symbols succeeded. |

See `references/methodology.md` for the calculation and non-claim rules.
