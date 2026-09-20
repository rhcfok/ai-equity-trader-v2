# Yahoo-Only Technical Model Reference

## Data Source

Use `GET /v8/finance/chart/{symbol}` from Yahoo Finance with `interval=1d`, `range=2y`, `includeAdjustedClose=true`, and `events=div,splits`. Require at least 252 complete daily OHLCV bars.

## Scoring Rules

| Section | Points | Deterministic Yahoo OHLCV test |
|---|---:|---|
| A: Location and structure | 0–4 | +2 if close is above both SMA50 and SMA200; +2 for a 30-session base with ≤20% high/low range and ≤12% net movement. |
| B: Energy build-up | 0–4 | +2 if SMA10/SMA20/SMA50 dispersion is ≤6%; +2 if ADX14 is below 20 or its five-session average is below the prior five-session average. |
| C: Participation and momentum | 0–4 | +2 if 10-session average share volume is ≤75% of the prior 20-session average; +2 if MACD histogram contracts toward zero or RSI14 is 45–55. |
| D: Activation and timing | 0–4 | +2 if close is above the prior 20-session high and anchored quarterly VWAP; +2 if current share volume is ≥1.5× the prior 20-session average. Subtract 3 section-D points if close exceeds the pivot by more than 5%. |

## Secondary Technical Labels

| Label | Calculation |
|---|---|
| `vcp_stage` | Identify four sequential 15-session contracting ranges. Return `mature` only with a base, contractions, and volume dry-up; return `breakout` only for an unextended confirmed breakout. |
| `pocket_pivot` | Current close exceeds prior close and current share volume exceeds every down-day volume in the preceding ten sessions. |
| `pull_back_price` | Highest available support among SMA20, anchored quarterly VWAP, and the 30-session base low. |
| `entry_price` | Prior 20-session high × 1.001. |
| `stop_loss` | Lower of 30-session base low × 0.99 and SMA50 × 0.99; apply a 7% fallback below entry if necessary. |
| `target_price` | Entry plus twice the entry-to-stop distance. |
| `pct_from_52w_high` | Latest close relative to the maximum high in the latest 252 bars. |

## Supabase Delivery

The existing result table receives legacy-compatible fields only. Keep calculated SMAs, ADX, MACD, VWAP, pivot, equity volume, and bar count in the local JSON audit artifact unless a schema migration is explicitly approved.

## Non-Negotiable Exclusions

Do not query, compute, display, or score: option volume, call/put ratios, open interest, implied volatility, IV percentile, option skew, ORATS fields, or any non-Yahoo market source.
