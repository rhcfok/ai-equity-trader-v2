# Option Opportunity Ledger — Data Contract

Read before changing the schema, the identity key, lifecycle states, or the win-rate formula. Conventions mirror `put-option-strategy/references/data_contract.md` (`csp-bps-opportunity`): a research-tracking ledger first, an outcome tracker second.

## Purpose

The daily Davis-playbook workflow (UW-option-breakout verdicts + normalized option-flow signals + JLaw option layer + GEX + live chains) publishes opportunities every trading day. `public."option-opportunity-davis"` stores one row per published opportunity and later receives terminal outcomes so the workflow can be measured in **winning rate** per strategy and period.

## Row identity and idempotency

`opportunity_key = report_date | strategy | symbol | expiry | short_strike` (pipe-joined, uppercase symbol). The daily runner merges explicitly — PATCH by `opportunity_key` first, INSERT only when no row matched — because PostgREST's `resolution=merge-duplicates,on_conflict=...` upsert is not honored on this table (identity PK `id` is `GENERATED ALWAYS` plus a separate unique constraint). Re-running the same day therefore merges instead of duplicating. Re-publishing a day refreshes the snapshot columns but must **never** clear a terminal `outcome` or `win`.

Contract symbols: `short_option_symbol` / `long_option_symbol` are OCC-style (e.g., `CRM261120P00230000`): symbol + YYMMDD + C/P + strike×1000 zero-padded to 8.

## Strategies

| strategy | side | legs |
|---|---|---|
| `SHORT_PUT` | put | short only (`long_*`, `spread_width` null) |
| `BULL_PUT_SPREAD` | put | short + long (long strike < short strike) |
| `BEAR_CALL_SPREAD` | call | short + long (long strike > short strike) |

## Lifecycle and outcomes

| status | meaning |
|---|---|
| `PROPOSED` | published by the daily run, not acted on |
| `ACTIVE` | user entered the structure |
| `CLOSED` | terminal; `outcome` must be filled |
| `ROLLED` | management roll; original row terminalized, successor logged as a new row |
| `ASSIGNED` | short put assigned (wheel continues); excluded from spread win-rate |
| `SKIPPED` | published but deliberately not taken |
| `NO_QUALIFIED_SETUP` | daily trace row (symbol `--`) when nothing passed the gates |

Terminal `outcome` values: `WON` (closed early for profit), `LOST` (closed for loss), `EXPIRED_WORTHLESS` (short OTM at expiry — full credit kept), `EXPIRED_UNTESTED` (never close to the money; excluded). `win` is set true only for `WON` and `EXPIRED_WORTHLESS`, false for `LOST`, null otherwise.

## Win-rate formula (the number this schema exists to produce)

```text
win_rate(strategy, period) =
    count(win = true) / count(outcome in ('WON', 'LOST', 'EXPIRED_WORTHLESS'))
```

`ASSIGNED`, `ROLLED`, `SKIPPED`, `EXPIRED_UNTESTED`, `NO_QUALIFIED_SETUP`, and rows still `PROPOSED`/`ACTIVE` are **excluded from the denominator**. Report them separately. Win-rate is a hypothetical-tracking metric, not executed-trade performance (same stance as the csp-bps-opportunity ledger).

## Economics conventions

Per share; multiply by 100 for one contract.

```text
entry_credit      = short_bid            (SHORT_PUT)
                  = short_bid - long_ask (spreads; require > 0)
collateral        = short_strike - entry_credit   (SHORT_PUT)
                  = spread_width - entry_credit   (spreads)
max_loss          = collateral           (SHORT_PUT)
                  = spread_width - entry_credit   (spreads)
return_on_risk    = entry_credit / max_loss
annualized_pct    = entry_credit / short_strike / dte * 365
davis_annualized_ok = annualized_pct >= 20            (Davis CSP filter)
delta_band_ok     = 0.20 <= |short_delta| <= 0.35     (Davis 20-30 delta band, 0.05 tolerance)
```

Reject a quote when bid or ask is absent/non-positive or ask < bid; mark `quote_status` accordingly and publish nothing for that leg.

## 21-DTE management line

Davis's universal rule is "decide at 21 DTE". The daily job should, for every `ACTIVE`/`PROPOSED` row reaching 21 calendar DTE, fill `dte21_mark_date`, `dte21_close_debit` (mid of closing legs), and `dte21_pnl = entry_credit - dte21_close_debit`. This column set is what makes the 21-DTE exit hypothesis itself backtestable.

## Signal evidence snapshot

- `verdict*`, `flow_*` from `flow_verdict_rows` (verdict_date) and `option_flow_symbol_signals` (latest signal_date) — normalized z-scores preferred over raw dollars.
- `jlaw_*` / `option_*` from `trade-jlaw-v2` latest run_date row.
- `gex_*` from `gex_snapshot` latest snapshot_ts.
- `earnings_clear` true only when `earnings_date` is strictly after `expiry` (J Law firewall: earnings < 2 weeks = no entry).

## Key reason codes (for `caution`)

- `VERDICT_MIXED` — direction not confirmed by flow verdicts
- `FLOW_CONFLICT` — normalized flow z-score opposes the trade direction
- `ANNUALIZED_BELOW_20` — Davis premium filter fails
- `DELTA_OUT_OF_BAND` — short delta outside 0.20–0.35
- `THIN_OI` — short-leg open interest below comfort threshold
- `EARNINGS_INSIDE_WINDOW` — earnings before expiry (only allowed for post-earnings entries)
- `POST_EARNINGS_ENTRY` — trade is only valid after the named print
