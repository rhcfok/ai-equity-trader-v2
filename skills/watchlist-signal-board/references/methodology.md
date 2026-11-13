# Watchlist Signal Board — Methodology

Reference for every rubric and mapping in the board. Change any gate here
only with a reason written down next to the change.

## 1. Fresh scan composite (max ≈ 9.5)

Computed live from yfinance OHLCV (`--period`, default 3mo). Indicators are
plain-pandas ports of the pandas-ta defaults used by the source module:
RSI(14) and ADX(14) via Wilder RMA smoothing, EMA/MACD seeded recursively
(SMA seed at first valid value, then `ewm(span, adjust=False)`).

| Check | Condition | Points |
|---|---|---:|
| Price vs SMA20 | above | +1.0 |
| Price vs SMA50 | above | +1.0 |
| RSI(14) | 50–70 | +1.0 |
| | 30–50 | +0.5 |
| | <30 | +0.25 |
| MACD(12,26,9) | line > signal | +1.0 |
| | histogram rising | +0.5 |
| EMA9/21 | golden cross | +0.5 |
| | death cross | −0.25 |
| Dual crossover | MACD + EMA both up, both ≤10 bars old | +1.0 |
| | both up, older | +0.5 |
| | both down, both ≤10 bars | −1.0 |
| | both down, older / conflicting | −0.5 |
| ADX(14) | >25 and +DI > −DI | +1.5 |
| | +DI > −DI only | +0.5 |
| Momentum | period return ÷ 20, clamped | −1.0 … +2.0 |

Direction bands: **BULL ≥ 4**, **BEAR ≤ 2**, else NEUT. Scores can go
negative (death crosses + ADX down + negative return). The score confirms
trends; it does not catch reversals or value.

## 2. jlaw-v2-yahoo (`trade-jlaw-v2`, review_type=yahoo)

Latest `run_date` row per symbol. `classification` ∈ {Skip, Watch, …} with
`jlaw_score` (0–14, even steps).

- Watch, or score ≥ 8 → **BULL** ("setup forming" — the actionable band)
- Skip / low score → **NEUT** (no entry setup; not a directional bet)

J Law scores measure *setup quality for entry*, not trend direction.

## 3. jlaw-v2-chart (`trade-jlaw-v2`, review_type=chart)

Latest `run_date` row per symbol. `classification` ∈ {Watch, Skip, Unreviewed}.

- Watch → **BULL**
- Skip → **NEUT**, plus a **chart-veto** flag when the yahoo row is BULL

Doctrine: *a failing chart always overrides a flattering score* — trend and
stage items are absolute. The veto never upgrades anything.

## 4. Option-flow verdict (`flow_verdict_rows`)

Latest `verdict_date` row per symbol.

- bullish → **BULL**, bearish → **BEAR**, mixed → **NEUT**,
  no-data/protective → **—**
- Stale (older than 7 days) → direction ignored, `flow-stale` flag set

## 5. quant2 (`quant2_leaderboard`)

Latest `run_name` board per ticker. A **weakness-repair LONG** model: active
rule flags (f_rsi_oversold, f_rsi_repair3, f_sma20_reclaim, …) on
beaten-down names, ranked per group.

- `n_active_rules ≥ 6` and `rank ≤ 100` → **BULL** (high-conviction repair setup)
- `n_active_rules ≥ 4` → **NEUT**
- else → **—**

A high quant2 rank on a BEAR-consensus row means "watch for reversal", not
"buy now". Its direction semantics differ from every other column.

## 6. stldc (`trade-stldc-v2`)

Latest `run_date` rows. `ldc_signal` ∈ {LONG, SHORT, NEUT} maps directly to
BULL / BEAR / NEUT. `st_dir` (supertrend) and `structure` are context in the
JSON; the SPY regime (mode of `spy_regime` across rows) heads the board.

## 7. Consensus & sorting

- Directional votes across the six columns; `—` (no opinion) excluded.
- Majority wins; ties resolve NEUT if any neutral vote, else `—`.
- Sort: net votes (bull − bear) desc, then scan score desc, then symbol.
- **CONFLICT** = at least one BULL and one BEAR vote. These rows are listed
  in a dedicated section — the disagreement is the deliverable, not noise.

## 8. Universe rules

`watchlist` categories core / satellite / watch1 / watch2 / watch3.
US option-listed exchanges only (NASDAQ, NYSE, AMEX, NYSEARCA/ARCA, BATS,
CBOE). NULL `exchange` kept for plain alphabetic tickers (no dot); explicit
non-US exchanges (OTC, LSE, Stockholm, XETR, Toronto, TSX) dropped. `euro`
category never scanned.
