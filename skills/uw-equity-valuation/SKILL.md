---
name: uw-equity-valuation
description: Build, run, validate, and report a daily Unusual Whales equity-valuation workflow for the Supabase watchlist. Use when ingesting UW fundamentals, forward estimates, valuation multiples, financial-strength metrics, reference valuations, or daily valuation snapshots for U.S.-listed watchlist tickers. Also use when computing mechanical valuation anchors (fair value, buy-below) from a second consensus source (Wind via ZCode MCP) with the Rijnberk-replica model, or when a short-put engine needs `buy_below` / `fair_value` anchors gated against GEX put walls.
---

# UW Equity Valuation

Use this skill to maintain the daily valuation layer for `public.watchlist`. It calculates and persists auditable financial metrics; it does not place trades or create investment orders.

## Data Contract

- **Supabase project:** `vmxxmdtzvwizrpjrrvnp` (`n8n_ai_pm`).
- **Universe:** `public.watchlist`; preserve the existing `symbol` and category data.
- **Daily snapshot:** `public.equity_valuation_uw_daily`, keyed by `(as_of_date, ticker)`.
- **Research reference:** `public.equity_valuation`, which stores letter-derived `price_target`, `buy_below`, and `fair_value` observations. Never overwrite it.
- **Second estimate source:** Wind consensus (via ZCode MCP `get_stock_fundamentals`) supplies FY1/FY2 consensus EPS, consensus target price, 5-year average P/E and its current percentile. Wind consensus is stored `est_source = 'wind_consensus'` with `est_basis = 'consensus_basis_unverified'` until a per-ticker GAAP/non-GAAP bridge check passes. It must never silently replace UW fields; it populates the `est_*`, `fy1_eps`, `fy2_eps`, and anchor columns.
- **Session date:** use the U.S. market session date; retain source timestamps independently.

Treat the daily UW snapshot as a separate, source-labeled layer. A missing field must remain `NULL` and be identified in `data_quality_flags`; never substitute remembered values. Foreign-currency and ADR-name consensus (e.g. ASML in EUR, TSM in TWD with a 5:1 ADR ratio, SONY in JPY) must be converted to USD before storage and flagged `fx_<CCY>`; SONY-class ADR-ratio ambiguity excludes the name from anchor math.

## Source Responsibilities

| Source | Use for |
|---|---|
| UW `get_company_info` | price, market cap, shares, sector, security type, next earnings date |
| UW statements / fundamentals | filing-based revenue, EPS, EBITDA, cash flow, cash, debt, equity, buybacks, SBC, segment context |
| UW company profile, when exposed | trailing/forward P/E, PEG, P/S, P/B, EV/revenue, EV/EBITDA, margins, ROA/ROE |
| UW forward earnings estimates, when exposed | FY1/FY2 EPS and revenue forecasts; revision snapshots |
| Wind consensus (ZCode MCP) | FY1/FY2 consensus EPS, consensus target price, 5-year average P/E and percentile; second-source layer behind `est_source` |
| `public.equity_valuation` | independent reference target, buy-below, fair-value, thesis, and source context |
| Deterministic code | all derived metrics, valuation labels, quality flags, and scores |

Do not treat analyst targets, UW forward estimates, or letter valuations as facts. Keep their dates, providers, periods, and currencies.

## Workflow

1. **Read the watchlist.** Resolve symbols and preserve their category. Include only verified U.S.-listed common stocks in the ranking universe. Keep excluded instruments in the snapshot with `is_us_listed = false`, `eligible_for_ranking = false`, and a flag.
2. **Retrieve source data.** Capture the current company record plus filing-based income statement, balance sheet, cash flow, fundamental breakdown, earnings history, and earnings date. Request profile and forward-estimate data only if the UW tools are exposed and authorized.
3. **Align periods.** Persist the latest filing date in `fundamentals_as_of_date`, estimate observation date in `estimates_as_of_date`, and price timestamp in `price_as_of`. Do not mix mismatched currencies or time periods.
4. **Calculate metrics deterministically.** Populate direct UW metrics only when supplied; otherwise calculate from aligned price, market-cap, statements, and estimates.
5. **Compute valuation anchors.** Run the Rijnberk-replica model (below, `calculation_version >= uw_rest_v1.4.0`) on every consensus-covered ticker; store `exit_mult`, `fair_value`, `buy_below`, the SBC-bridge variants, and `anchor_verdict`. Names that fail a gate get `anchor_verdict = 'no_anchor'` plus a flag — never a fabricated number.
6. **Join research references point-in-time.** For each ticker, select the latest `public.equity_valuation` row with `post_date <= as_of_date`, separately by `valuation_type`. Preserve the source, currency, and date in the `reference_*` fields. Flag `ref_stale` when the reference post_date is older than 90 days.
7. **Apply quality gates.** Use `complete`, `partial`, `insufficient`, `stale`, or `failed`. Add granular JSON strings to `data_quality_flags` such as `missing_forward_estimates`, `negative_ttm_eps`, `unverified_us_exchange`, `stale_filing_data`, `currency_mismatch`, `no_valuation_anchor`, or `consensus_pt_available`.
8. **Upsert idempotently.** Upsert one row per `(as_of_date, ticker)` and update `updated_at`. Batch upserts must carry an identical key set in every object (PostgREST `PGRST102` otherwise). Preserve audit metadata in `source_metadata`; never store API keys or other secrets.
9. **Report coverage.** State the ticker count, verified U.S.-listed count, full-versus-partial metric coverage, anchor verdict distribution, stale or failed calls, and the date/period basis of results.

## Calculation Rules

Use decimal fractions for percentages in storage: `0.15` represents 15%.

| Metric | Formula / rule |
|---|---|
| Trailing P/E | `price / TTM diluted EPS`; null when EPS is non-positive or unavailable |
| Forward P/E | `price / NTM EPS`; null when NTM EPS is non-positive or unavailable |
| PEG | `forward_pe / FY2 EPS growth percentage`; null when either input is non-positive |
| P/S | `market_cap / TTM revenue` |
| P/B | `market_cap / latest shareholder equity`; null when equity is non-positive |
| EV | `market_cap + total_debt - cash_and_equivalents` when an aligned direct EV is unavailable |
| EV/Revenue | `enterprise_value / TTM revenue`; null when revenue is non-positive |
| EV/EBITDA | `enterprise_value / TTM EBITDA`; null when EBITDA is non-positive |
| Free cash flow | operating cash flow minus capital expenditure |
| FCF yield | `TTM free cash flow / market_cap`; null when market cap is non-positive |
| Net debt | total debt minus cash and equivalents |
| Net debt / EBITDA | `net_debt / TTM EBITDA`; null when EBITDA is non-positive |
| EPS revision | percent change in stored FY1/FY2 estimate observations over the stated period; do not infer a revision on the first observation |

Use `not_meaningful` for loss-making or denominator-invalid cases. Do not replace invalid ratios with zero.

## Valuation Anchors (Rijnberk-replica, `calculation_version >= uw_rest_v1.4.0`)

Reference implementation: `scripts/anchor_model_v1_4.py`. The model replicates the Rijnberk InvestInsights methodology mechanically: an exit multiple on FY2 consensus EPS, discounted back at a required return, with a fixed buy-below ratio. Calibrated against Daan's published rows (TSM fair 348 vs his 350; AVGO buy-below 392 vs his 390; TXN fair 150 vs his 150).

Inputs per ticker: Wind FY1/FY2 consensus EPS (USD-converted), current price, sector medians of FY2 P/E and FY2 growth from the same consensus pull (n >= 4 peers else `peer_set_small`), 5-year average P/E and percentile.

```
fy2_growth      = fy2_eps / fy1_eps - 1                      (requires both > 0)
justified_mult  = min(1.0 * min(fy2_growth, 2 * sector_median_growth) * 100, 30)
hist_cap        = pe_5y_avg * 0.9                            (forward/trailing basis adj; ignore if > 300)
floor           = 0 if cyclical_peak_risk else sector_median_fy2_pe
exit_mult       = clamp(justified_mult, floor, min(30, max(hist_cap, floor)))
target          = exit_mult * fy2_eps
fair_value      = target / 1.15 ^ 2.25                       (15%/yr required return)
buy_below       = 0.70 * fair_value
fair_value_adj  = exit_mult * (fy2_eps + SBC_ttm/shares) / 1.15 ^ 2.25   (sanity: fy2_adj <= 3 * fy2_eps)
buy_below_adj   = 0.70 * fair_value_adj
anchor_verdict  = accumulate if price <= buy_below; watch if price <= fair_value; else expensive
```

Gates that force `anchor_verdict = 'no_anchor'`:

- Missing or non-positive FY1/FY2 consensus EPS (`negative_consensus_eps` when present but non-positive).
- SBC-heavy names where GAAP-consensus EPS is not a valid basis (CRWD/ARM-class): `no_valuation_anchor` until adjusted consensus is available; the SBC bridge alone does not recover the gap.
- FX/ADR ambiguity that cannot be resolved (SONY-class).
- `cyclical_peak_risk` (trailing P/E < 12): the name keeps an anchor only with the median floor removed and the flag set; treat memory/energy verdicts as cycle-dependent, never as put-strike floors without review.

`pe_5y_pctile` is stored as context (e.g. MRVL at the 89th percentile of its own 5-year history) and never used as a gate. `consensus_pt` (Wind aggregate target price) is stored as a sentiment ceiling, not a floor.

Downstream composition (options engine): `buy_below_final = min(buy_below, reference_buy_below when fresh)`, and an eligible short-put strike K must satisfy `gex_put_wall <= K <= buy_below_final`; when `gex_put_wall > buy_below_final`, flag `put_wall_above_buy_threshold` and skip the ticker.

## Valuation Labels

Assign labels only after material inputs pass quality gates:

- `cheap`, `fair_low`, `fair_high`, `expensive`: label from documented deterministic percentile or peer-comparison rules.
- `not_meaningful`: key valuation denominator is non-positive.
- `insufficient_data`: inputs, estimates, exchange verification, or period alignment are insufficient.

Record the methodology version in `calculation_version`. Do not change label thresholds without a version bump and a documented rationale.

`valuation_category` (trailing-PE bands) and `anchor_verdict` (forward anchor model) are independent fields and may disagree; both are stored, and the disagreement itself is information (e.g. MRVL is `expensive` on both, while a hyper-growth name can be `expensive` on trailing bands but `watch` on the forward anchor).

## Storage Notes

Second-source and anchor columns (added `uw_rest_v1.4.0`): `est_source`, `est_currency`, `est_basis`, `exit_mult`, `fair_value`, `buy_below`, `fair_value_adj`, `buy_below_adj`, `pe_5y_avg`, `pe_5y_pctile`, `consensus_pt`, `anchor_verdict`, `scenario_assumptions` (jsonb: model version, discount rate, horizon, buy-below ratio, caps, computed-at). `fy1_eps`, `fy2_eps`, and `estimates_as_of_date` predate this change and are populated from the second source when UW estimates are unavailable.

`source_metadata` should include endpoint names, retrieval timestamps, filing accession or period identifiers, and estimate observation information. Keep raw payloads out of the main metric columns unless the payload is necessary for auditability and has no credentials.

The table has RLS enabled. Use the authorized Supabase connector/service role for ingestion. Use `apply_migration` for schema changes and read queries with explicit `LIMIT`s for checks.

## Completion Checks

Before reporting a successful run:

1. Confirm one current-session snapshot per processed ticker.
2. Confirm no duplicate `(as_of_date, ticker)` rows.
3. Confirm source timestamps and fundamental/estimate periods are present when their metrics are populated.
4. Confirm forward P/E and PEG are null plus flagged when authorized forward estimates are unavailable.
5. Confirm the existing `public.equity_valuation` table was only read, not changed.
6. Confirm every anchor row carries `est_source`, `estimates_as_of_date`, and `scenario_assumptions`, and that `fair_value` is `NULL` exactly when `anchor_verdict = 'no_anchor'`.
7. Confirm FX/ADR-converted consensus rows carry `fx_<CCY>` flags and that batch upserts completed without `PGRST102` key-mismatch errors.
8. Report all source or validation failures explicitly.
