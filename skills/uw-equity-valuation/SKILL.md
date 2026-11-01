---
name: uw-equity-valuation
description: Build, run, validate, and report a daily Unusual Whales equity-valuation workflow for the Supabase watchlist. Use when ingesting UW fundamentals, forward estimates, valuation multiples, financial-strength metrics, reference valuations, or daily valuation snapshots for U.S.-listed watchlist tickers.
---

# UW Equity Valuation

Use this skill to maintain the daily valuation layer for `public.watchlist`. It calculates and persists auditable financial metrics; it does not place trades or create investment orders.

## Data Contract

- **Supabase project:** `vmxxmdtzvwizrpjrrvnp` (`n8n_ai_pm`).
- **Universe:** `public.watchlist`; preserve the existing `symbol` and category data.
- **Daily snapshot:** `public.equity_valuation_uw_daily`, keyed by `(as_of_date, ticker)`.
- **Research reference:** `public.equity_valuation`, which stores letter-derived `price_target`, `buy_below`, and `fair_value` observations. Never overwrite it.
- **Session date:** use the U.S. market session date; retain source timestamps independently.

Treat the daily UW snapshot as a separate, source-labeled layer. A missing field must remain `NULL` and be identified in `data_quality_flags`; never substitute remembered values.

## Source Responsibilities

| Source | Use for |
|---|---|
| UW `get_company_info` | price, market cap, shares, sector, security type, next earnings date |
| UW statements / fundamentals | filing-based revenue, EPS, EBITDA, cash flow, cash, debt, equity, buybacks, SBC, segment context |
| UW company profile, when exposed | trailing/forward P/E, PEG, P/S, P/B, EV/revenue, EV/EBITDA, margins, ROA/ROE |
| UW forward earnings estimates, when exposed | FY1/FY2 EPS and revenue forecasts; revision snapshots |
| `public.equity_valuation` | independent reference target, buy-below, fair-value, thesis, and source context |
| Deterministic code | all derived metrics, valuation labels, quality flags, and scores |

Do not treat analyst targets, UW forward estimates, or letter valuations as facts. Keep their dates, providers, periods, and currencies.

## Workflow

1. **Read the watchlist.** Resolve symbols and preserve their category. Include only verified U.S.-listed common stocks in the ranking universe. Keep excluded instruments in the snapshot with `is_us_listed = false`, `eligible_for_ranking = false`, and a flag.
2. **Retrieve source data.** Capture the current company record plus filing-based income statement, balance sheet, cash flow, fundamental breakdown, earnings history, and earnings date. Request profile and forward-estimate data only if the UW tools are exposed and authorized.
3. **Align periods.** Persist the latest filing date in `fundamentals_as_of_date`, estimate observation date in `estimates_as_of_date`, and price timestamp in `price_as_of`. Do not mix mismatched currencies or time periods.
4. **Calculate metrics deterministically.** Populate direct UW metrics only when supplied; otherwise calculate from aligned price, market-cap, statements, and estimates.
5. **Join research references point-in-time.** For each ticker, select the latest `public.equity_valuation` row with `post_date <= as_of_date`, separately by `valuation_type`. Preserve the source, currency, and date in the `reference_*` fields.
6. **Apply quality gates.** Use `complete`, `partial`, `insufficient`, `stale`, or `failed`. Add granular JSON strings to `data_quality_flags` such as `missing_forward_estimates`, `negative_ttm_eps`, `unverified_us_exchange`, `stale_filing_data`, or `currency_mismatch`.
7. **Upsert idempotently.** Upsert one row per `(as_of_date, ticker)` and update `updated_at`. Preserve audit metadata in `source_metadata`; never store API keys or other secrets.
8. **Report coverage.** State the ticker count, verified U.S.-listed count, full-versus-partial metric coverage, stale or failed calls, and the date/period basis of results.

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

## Valuation Labels

Assign labels only after material inputs pass quality gates:

- `cheap`, `fair_low`, `fair_high`, `expensive`: label from documented deterministic percentile or peer-comparison rules.
- `not_meaningful`: key valuation denominator is non-positive.
- `insufficient_data`: inputs, estimates, exchange verification, or period alignment are insufficient.

Record the methodology version in `calculation_version`. Do not change label thresholds without a version bump and a documented rationale.

## Storage Notes

`source_metadata` should include endpoint names, retrieval timestamps, filing accession or period identifiers, and estimate observation information. Keep raw payloads out of the main metric columns unless the payload is necessary for auditability and has no credentials.

The table has RLS enabled. Use the authorized Supabase connector/service role for ingestion. Use `apply_migration` for schema changes and read queries with explicit `LIMIT`s for checks.

## Completion Checks

Before reporting a successful run:

1. Confirm one current-session snapshot per processed ticker.
2. Confirm no duplicate `(as_of_date, ticker)` rows.
3. Confirm source timestamps and fundamental/estimate periods are present when their metrics are populated.
4. Confirm forward P/E and PEG are null plus flagged when authorized forward estimates are unavailable.
5. Confirm the existing `public.equity_valuation` table was only read, not changed.
6. Report all source or validation failures explicitly.
