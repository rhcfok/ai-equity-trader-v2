# option-davis — Methodology Reference

Read before changing candidate scoring, gates, economics, or the ledger behavior.

## Signal sources

| Input | Table / source | Role |
|---|---|---|
| Flow verdicts | `flow_verdict_rows` (latest `verdict_date`) | Directional gate: bullish/mixed feed the bull side, bearish feeds the bear side; `earnings_block` and `next_earnings_date` drive the earnings firewall. |
| Normalized flow scores | `option_flow_symbol_signals` (latest `signal_date`) | `bullish_z` / `bearish_z` / `net_bias_z` (per-day cross-section) — comparable across symbols, unlike raw dollar scores. |
| JLaw option layer | `trade-jlaw-v2` (latest `run_date`) | `option_score`, `option_tier`, `option_alert`, `option_iv30`, `option_iv_rank`, `option_iv_trend`, `option_put_call_ratio`, `option_pct_below_high`, plus `jlaw_score` / `classification` / `gates`. Rows with NULL `option_score` are excluded (option layer not applied yet). |
| Dealer positioning | `gex_snapshot` (latest snapshot date) | Put wall = support anchor for short puts; call wall = resistance anchor for bear calls; `net_gex_mm`, `gravity`. |
| Dark pool enricher | `darkpool_symbol_daily` (latest `fetch_date`) | `net_premium` / `buy_premium` / `sell_premium` / `total_premium`, `block_count`, `top_level(_premium)`, `accumulating` — institutional accumulation confirmation for the bull side and distribution confirmation for the bear side. Missing rows = `DP_NO_DATA` caution, never a failure. |
| Quotes | Unusual Whales `/api/stock/{t}/option-contracts` + `/quote` | NBBO bid/ask, delta, IV, OI per contract; canonical quote source. |

## Candidate scoring (bull side)

0–15 composite, deterministic ordering only — not a probability or a sizing instruction.

| Component | Points |
|---|---:|
| Verdict bullish (mixed = 1) | 3 |
| jlaw_score ≥ 9 (≥ 7 = 1.5) | 3 |
| Classification Watch | 1 |
| option_alert | 2 |
| option_tier MODERATE/STRONG | 1.5 |
| Flow bias bullish | 2 |
| net_bias_z ≥ 1 | 1 |
| GEX put wall 0.5–6% below spot | 1.5 |
| IV rank 20–80 | 1 |
| Earnings after expiry | 1 |
| Dark pool accumulating | 1.5 |
| gates.trend_ok / gates.near_52w_high | 0.5 each |

Pre-filters before scoring: verdict not bearish, no earnings block, option layer present, IV rank 15–85, spot available. Bear side is ranked by `bearish_z` descending with dark-pool distribution (`dp_accumulating = false`) as the tiebreak.

## Dark pool ledger mapping

When a `darkpool_symbol_daily` row exists for the symbol, the opportunity row carries:

```text
dp_as_of               = window_end (fallback fetch_date)
dp_total_premium_mm    = total_premium / 1e6
dp_buy_mm / dp_sell_mm = buy_premium / sell_premium / 1e6
dp_net_mm              = net_premium / 1e6
dp_blocks              = block_count (integer)
dp_top_level(_mm)      = top_level / top_level_premium / 1e6
dp_accumulating        = accumulating (boolean)
```

When no row exists the `dp_*` keys are written as explicit NULLs (the daily aggregate table is the single source of truth — legacy values from the old patch mode are cleared on the first re-run) and the row gets the `DP_NO_DATA` caution instead. `DP_DISTRIBUTION` is added to non-bear strategies when `dp_accumulating` is false.

## Structure selection

- **Short put:** nearest put with \|delta\| ≈ 0.30 below spot; 20Δ put reported as the alternative. Logged as `SHORT_PUT`.
- **Bull put spread:** 5-wide, short = the 30Δ put, long = short − 5, only when net credit > 0. Logged as `BULL_PUT_SPREAD`.
- **Bear call spread:** nearest call with delta ≈ 0.30 above spot; 20Δ alternative; 5-wide long = short + 5. Logged as `BEAR_CALL_SPREAD`.

## Economics (per share; x100 per contract)

```text
entry_credit      = short_bid            (SHORT_PUT)
                  = short_bid − long_ask (spreads; require > 0)
collateral        = short_strike − credit   (SHORT_PUT)
                  = width − credit          (spreads)
max_loss          = collateral / width − credit
return_on_risk    = credit / max_loss
annualized_pct    = credit / short_strike / dte × 365
davis_annualized_ok = annualized_pct ≥ 20
delta_band_ok     = 0.20 ≤ |short_delta| ≤ 0.35
```

Contract symbols are OCC-style (`CRM261120P00230000`): symbol + YYMMDD + C/P + strike×1000 zero-padded to 8.

## Win-rate measurement

See `skills/UW-option-breakout/references/option_opportunity_contract.md` — the ledger's terminal `outcome`/`win` columns and the `dte21_*` marks exist to compute per-strategy win rates; the runner only writes `PROPOSED` rows and never touches outcome columns.
