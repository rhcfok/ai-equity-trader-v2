# UW Options Income Engine — Methodology & Data Contract

Read this reference before modifying strategy routing gates, Unusual Whales valuation/fundamental logic, strike selection, confidence scoring, or ledger tracking.

## 1. Design Principles

1. **100% Stateless (Zero Portfolio Dependency):** The engine never reads `watchlist.position` or brokerage holdings. Every strategy (`WHEEL_CSP`, `WHEEL_CC`, `BULL_PUT_SPREAD`, `SHORT_STRANGLE`, `DIAGONAL_SPREAD`, `BEAR_CALL_SPREAD`) is evaluated purely on market structure, Unusual Whales data, J Law levels, GEX walls, and Dark Pool accumulation/distribution.
2. **Unusual Whales Replaces `equity_valuation_signal_daily`:** Fundamental valuation, earnings growth, surprise momentum, and analyst target anchors are computed directly from Unusual Whales REST endpoints (`/api/stock/{ticker}/earnings`, `/api/screener/analysts`, `/api/companies/{ticker}/profile` with automatic standard-tier fallback).
3. **Regime & IV Routing First, Contract Selection Second:** Instead of forcing every ticker into a single DTE or spread width, the router classifies each ticker's **IV Percentile + Term Structure** and **Directional / GEX Regime**, then routes it to the appropriate option structure.
4. **Triple Support Floor & Strike-Level Sweep Veto:** Short put strikes must sit at or below the applicable structural support floor (`min(J Law stop, GEX put wall, Dark Pool top level)` when valid) and are hard-vetoed if `/api/option-trades` shows aggressive ask-side put sweeps (`size > OI`) within ±3% of the candidate strike.

---

## 2. Unusual Whales Fundamental & Valuation Replacement

Because `equity_valuation_signal_daily` is retired, the runner computes `uw_valuation_tier` and `wheel_fundamental_ok` per symbol using only endpoints on the `unusual-whales-api` strict whitelist (`/api/stock/{ticker}/earnings` and `/api/stock/{ticker}/financials`):

1. **Earnings & TTM EPS (`/api/stock/{ticker}/earnings`):**
   - Filter rows with `report_type == "quarterly"` and valid `reported_eps`, sorted by `fiscal_date_ending` descending.
   - `uw_ttm_eps` = sum of the 4 most recent quarters' `reported_eps`.
   - `uw_prior_ttm_eps` = sum of quarters 5–8 `reported_eps` (when available).
   - `uw_eps_growth_yoy_pct` = `((uw_ttm_eps - uw_prior_ttm_eps) / abs(uw_prior_ttm_eps)) * 100` when `uw_prior_ttm_eps > 0`.
   - `uw_latest_surprise_pct` = most recent quarter's `surprise_percentage`.
   - `uw_pe_ratio` = `spot / uw_ttm_eps` when `uw_ttm_eps > 0`.
   - `uw_peg_ratio` = `uw_pe_ratio / uw_eps_growth_yoy_pct` when both are positive.
2. **Financial Statements Supplement (`/api/stock/{ticker}/financials`, `/api/stock/{ticker}/income-statements`, `/api/stock/{ticker}/cash-flows`):**
   - Confirms positive operating income and free cash flow trends when needed, while J Law `target_price` from `trade-jlaw-v2` (or optional analyst targets if passed via fixture) serves as the upside target anchor.
3. **Deterministic Valuation Tier (`uw_valuation_tier`):**
   - `not_meaningful`: `uw_ttm_eps is None` or `uw_ttm_eps <= 0`
   - `cheap`: `uw_ttm_eps > 0` AND (`uw_pe_ratio <= 18` OR `uw_peg_ratio <= 1.2` OR (`uw_analyst_target` and `spot <= uw_analyst_target * 0.85`))
   - `fair_low`: `uw_ttm_eps > 0` AND (`uw_pe_ratio <= 28` OR `uw_peg_ratio <= 2.0` OR (`uw_analyst_target` and `spot <= uw_analyst_target * 0.95`))
   - `fair_high`: `uw_ttm_eps > 0` AND (`uw_pe_ratio <= 40` OR (`uw_analyst_target` and `spot <= uw_analyst_target * 1.05`))
   - `expensive`: `uw_ttm_eps > 0` and above `fair_high` thresholds

---

## 3. Strategy Router & Mechanical Hard Gates

| Strategy | DTE Bucket | Target DTE | Delta Band | Required Regime, IV, & Fundamental Gates | Strike & Economics Gates |
|---|---|---:|---|---|---|
| `WHEEL_CSP` | `CORE` | 25–39d (~32d Friday) | `0.18 <= \|Δ\| <= 0.28` | - US-listed `core`, `satellite`, or `watch1`<br>- J Law `Valid` or `Watch` with positive `stop_loss`<br>- `verdict` in `{"bullish", "mixed"}`<br>- `uw_ttm_eps > 0`, `not recent_downgrade_14d`, `uw_valuation_tier` in `{"cheap", "fair_low", "fair_high"}`<br>- `uw_iv_rank >= 25%`, `uw_term_ratio_7_30 <= 1.25`<br>- `32 <= uw_rsi_14 <= 60` (Davis pullback / non-overbought rule)<br>- `dp_accumulating != False` | - `short_strike <= safety_floor`<br>- `annualized_pct >= 20%`<br>- `strike_adverse_sweeps == 0`<br>- Earnings strictly after expiry |
| `WHEEL_CSP` | `TACTICAL` | 3–10d | `\|Δ\| < 0.20` | - Same structural & earnings gates as `CORE`, plus `uw_term_ratio_7_30 <= 1.18` (no front-week event shock) | - `short_strike <= safety_floor`<br>- `annualized_pct >= 18%`<br>- `strike_adverse_sweeps == 0` |
| `WHEEL_CC` | `CORE` | 14–35d | `0.16 <= Δ <= 0.28` | - Stateless covered-call reference board for `core` & `satellite` symbols<br>- Fires when `uw_rsi_14 >= 60`, OR `covered-call-selling` in `verdict` caveats, OR `verdict == "mixed"` near Call Wall<br>- `uw_iv_rank >= 20%` | - `short_strike >= safety_ceiling` (`max(gex_call_wall, jlaw_target_price)` when available, else `spot * 1.04`)<br>- `strike_adverse_sweeps == 0` (no bullish ask-side call sweeps blowing through `short_strike`)<br>- Earnings strictly after expiry |
| `BULL_PUT_SPREAD` | `CORE` | 14–35d | `0.18 <= \|Δ_short\| <= 0.28` | - J Law `Valid` or `Watch` (`jlaw_score >= 8`)<br>- `verdict` in `{"bullish", "mixed"}`<br>- `uw_iv_rank >= 30%`<br>- `dp_accumulating != False` | - `short_strike <= min(jlaw_stop_loss, gex_put_wall)`<br>- Width = **$5** if `spot < $150`, **$10** if `spot >= $150`<br>- `0.20 <= return_on_risk <= 0.35` from strict NBBO (`short_bid - long_ask`)<br>- `strike_adverse_sweeps == 0`<br>- Earnings strictly after expiry |
| `SHORT_STRANGLE` | `STRANGLE` | 28–45d | `0.12 <= \|Δ_put\|, Δ_call <= 0.20` | - **Positive GEX Pin Regime:** `gex_regime == "positive"` (`gex_net_mm > 0`)<br>- **High IV:** `uw_iv_rank >= 45%`<br>- **Range-Bound:** `38 <= uw_rsi_14 <= 62` and `verdict` in `{"mixed", "no-data"}` & `\|flow_composite\| < 30`<br>- Corridor width `(gex_call_wall - gex_put_wall) / spot >= 0.08` | - `short_put_strike <= gex_put_wall` (and `<= dp_top_level` when accumulating)<br>- `short_call_strike >= gex_call_wall`<br>- Combined NBBO credit `short_put_bid + short_call_bid > 0` and `annualized_pct >= 18%`<br>- Zero adverse sweeps on both legs; earnings clear |
| `DIAGONAL_SPREAD` | `DIAGONAL` | Back: 60–95d<br>Front: 7–21d | Back: `0.70 <= Δ <= 0.82`<br>Front: `0.20 <= Δ <= 0.30` | - **Low-IV Bullish Breakout:** J Law `Valid` (or `Watch` with `jlaw_score >= 11`)<br>- `verdict == "bullish"` and `dp_accumulating == True` and `flow_label` in `{"bull", "strong_bull"}` and no `flow_conflicts`<br>- `uw_iv_rank < 38%` (where credit spreads underpay)<br>- `uw_term_ratio_30_60 <= 1.08` (no severe front-month IV crush risk) | - Front short call strike `>= gex_call_wall` (or `>= spot * 1.03`)<br>- Net debit = `long_back_ask - short_front_bid`<br>- **No Upside Inversion Gate:** `net_debit <= 0.78 * (short_front_strike - long_back_strike)`<br>- Back-month extrinsic value `<= 25%` of `long_back_ask`<br>- Earnings strictly after front-month expiry |
| `BEAR_CALL_SPREAD` | `CORE` | 14–35d | `0.18 <= Δ_short <= 0.28` | - (`verdict == "bearish"` and (`flow_composite <= -15` or `dp_accumulating == False`)) or `flow_label == "strong_bear"`<br>- `uw_iv_rank >= 30%` | - `short_strike >= gex_call_wall` (when valid)<br>- Width = **$5** if `spot < $150`, **$10** if `spot >= $150`<br>- `return_on_risk >= 0.20` |

---

## 4. Ranked Confidence Score (0–100)

Every structure that passes its mechanical hard gates receives a deterministic 0–100 Confidence Score across 7 pillars:

1. **J Law Structure & Stop Buffer (0–20 pts):** `Valid` vs `Watch`, `jlaw_score`, and percentage distance between `spot`, `safety_floor`, and `short_strike`.
2. **Combined Unusual Flow (0–20 pts):** `10 + 8 × alignment(strategy, flow_composite)`; −3 when `flow_conflicts` is non-empty (directional strategies), +2 for institutional bid-side writing at the strike (`strike_whale_put_bids > 0`), −8 for adverse sweeps. Alignment = `flow_composite/100` for CSP / BPS / Diagonal, `−flow_composite/100` for CC / BCS, `1 − 2·min(1, |c|/50)` for Strangle (neutral flow is best). See §4a.
3. **GEX Map & Corridor Quality (0–15 pts):** Valid `positive` GEX regime, distance of `gex_put_wall` / `gex_call_wall`, and whether strikes sit outside the dealer walls.
4. **UW Fundamental & Analyst Quality (0–15 pts):** `uw_valuation_tier` (`cheap` / `fair_low`), positive `uw_latest_surprise_pct`, and favorable `uw_analyst_recent_action`.
5. **Volatility & Term-Structure Fit (0–10 pts):** Match between strategy type and `uw_iv_rank` + `uw_term_ratio_30_60` + `uw_stance_fit_score`.
6. **Execution & Liquidity Quality (0–10 pts):** Open interest depth and tight NBBO bid-ask spread percentage on all legs.
7. **Economics & Timing Quality (0–10 pts):** Return on risk / annualized yield and daily `RSI(14)` pullback alignment.

### 4a. Combined Unusual-Flow Score (`compute_flow_composite`)

One −100…+100 score per ticker, fusing four sources that previously sat side by side unreconciled:

| Leg | Weight | Source | Value in [−1, 1] |
|---|---:|---|---|
| `verdict` | 35% | `flow_verdict_rows` | bullish +1 / bearish −1 / protective −0.5 / covered-call-selling −0.3 / mixed 0, × alert confidence. When the uncapped stats from `UW-option-breakout/scripts/flow_alert_stats.py` are on the row (`alert_count_total`, `premium_tilt`): `0.5 × sign × clip(alert_count_total/40, 0.3, 1) + 0.5 × premium_tilt`. Otherwise legacy `sign × clip(alert_count/10, 0.3, 1)` (agent sample capped at 20). Verdicts older than 4 calendar days (`VERDICT_STALE_DAYS`) are dropped and the row's `verdict` is demoted to `no-data` with caution `VERDICT_STALE` |
| `premium_today` | 30% | latest `option_flow_symbol_signals` row | premium tilt = `sign(net_directional_premium) × directional_confidence × size`, size = `clip(log10(unusual_premium $M)/2, 0.3, 1)` |
| `premium_3d` | 20% | last 3 signal dates | mean tilt (missing days = 0), i.e. persistence |
| `darkpool` | 15% | `darkpool_symbol_daily` | `clip(1.5 × net_premium / total_premium)` |

- **Why tilt, not `net_bias_z`:** until 2026-10-29 `net_bias_z` was a plain mean/stdev cross-sectional z-score of dollar net premium, which one outsized name dominated (2026-10-28: MU z = +6.66 pushed ~50 of 61 tickers to about −0.27). It is now a robust z (signed-log premium, median/MAD, ±5 clip; MU +2.16, META −2.73 on 10-28), but the per-ticker tilt is still used here because it does not depend on who else is in the day's cross-section. `net_bias_z` is stored for reference.
- **Coverage:** missing options legs are dropped and the remaining options weights renormalised to 85%. Dark pool keeps a fixed 15% and **on its own can move the score by at most ±15 and leaves the label `neutral`**. Flow signals older than 5 calendar days are treated as missing.
- **Labels:** `strong_bull ≥ 40`, `bull ≥ 15`, `neutral`, `bear ≤ −15`, `strong_bear ≤ −40`.
- **Conflicts (`flow_conflicts`, also `caution = FLOW_CONFLICT_*`):** `verdict_vs_premium` (verdict bullish/bearish but today's tilt points the other way with |tilt| ≥ 0.4, e.g. MU 2026-10-28 bearish verdict vs +$448M bullish premium); `darkpool_vs_options` (|darkpool| ≥ 0.5 and opposite to the options side, e.g. META 2026-10-28 bullish verdict vs −$2.85B dark pool); `verdict_vs_alert_premium` (agent verdict and the deterministic `verdict_quant` from the full alert tape point in opposite directions; protective counts as bearish).
- **Gates using the composite:** `strong_bear` vetoes `WHEEL_CSP` and `BULL_PUT_SPREAD`; `DIAGONAL_SPREAD` needs `bull`/`strong_bull` and no conflicts; `SHORT_STRANGLE` needs `|flow_composite| < 30`; `WHEEL_CC` also opens on `bear`/`strong_bear`; `BEAR_CALL_SPREAD` as in §3.
- **Persisted columns:** `flow_alert_count`, `flow_composite`, `flow_label`, `flow_conflicts`, `flow_components` (leg values + `missing` + `flow_signal_date` + `verdict_date` / `verdict_stale` / `verdict_quant` / `alert_count_total` / `alert_premium_tilt`).

Score bands:
- **85–100:** `STRONG`
- **70–84:** `HIGHER`
- **60–69:** `MODERATE`
- **< 60:** `LOWER` (excluded from default Top-20 publication unless `--include-lower-band` is passed, fixing the prior `put-option-strategy` unfiltered publication gap).

---

## 5. Automated Lifecycle & Win-Rate Tracking

The runner includes a `--track-open` pass (enabled by default in daily runs) that evaluates all `PROPOSED` and `ACTIVE` rows in `public."uw-option-opportunities"`:

1. **50% Max-Profit Auto-Win (`outcome = 'WON'`, `win = true`):**
   - For credit strategies (`WHEEL_CSP`, `WHEEL_CC`, `BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`, `SHORT_STRANGLE`), if the conservative NBBO closing debit (`short_ask` for single legs, `short_ask - long_bid` for verticals, `short_put_ask + short_call_ask` for strangles) is `<= 0.50 * entry_credit`, the row is marked `CLOSED` / `WON`.
   - For `DIAGONAL_SPREAD`, if current spread mark (`long_back_bid - short_front_ask`) is `>= 1.25 * entry_debit` (or short front call decays `>= 70%`), the row is marked `CLOSED` / `WON`.
2. **21-DTE Davis Decision Checkpoint:**
   - When `short_dte <= 21` and `dte21_mark_date` is null, records `dte21_mark_date`, `dte21_close_mark`, and `dte21_pnl`.
3. **Structural Defense Alert (`defense_reason`):**
   - If the latest `gex_put_wall` drops below `short_strike` (on put structures) or `spot` breaches `short_strike` with adverse sweeps, sets `defense_trigger_date` and `defense_reason = 'GEX_WALL_BREACH'` so the daily report highlights immediate roll/close candidates before 21 DTE.
4. **Expiry Settlement:**
   - On or after `short_expiry`, settles remaining open rows at intrinsic value (`EXPIRED_WORTHLESS` -> `win = true`, or `LOST` -> `win = false` if net P&L < 0).
