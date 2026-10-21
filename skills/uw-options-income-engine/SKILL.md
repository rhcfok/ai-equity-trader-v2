---
name: uw-options-income-engine
description: Run a stateless multi-strategy options income engine combining option-davis and put-option-strategy with Unusual Whales earnings/financial valuation, IV term-structure, RSI(14), GEX walls, Dark Pool levels, and strike-level sweep vetoes across Wheel (CSP + CC), Bull Put Spread, Short Strangle, Diagonal Spread, and Bear Call Spread.
---

# UW Options Income Engine (`uw-options-income-engine`)

Use this skill when generating, auditing, or extending the daily **stateless multi-strategy options income board** (`WHEEL_CSP`, `WHEEL_CC`, `BULL_PUT_SPREAD`, `SHORT_STRANGLE`, `DIAGONAL_SPREAD`, `BEAR_CALL_SPREAD`) powered by Unusual Whales REST endpoints and the Supabase trading stack (`watchlist`, `trade-jlaw-v2`, `flow_verdict_rows`, `option_flow_symbol_signals`, `gex_snapshot`, `darkpool_symbol_daily`).

Read `references/methodology.md` before changing routing gates, valuation tiers, strike selection, confidence scoring, or lifecycle rules.

---

## Critical Architectural Rules

0. **Scan Universe = `public.watchlist` core / satellite / watch1 only:**
   - Query `watchlist?select=symbol,category,exchange&category=in.(core,satellite,watch1)` (server-side filter). Never scan `watch2`, `watch3`, `euro`, or any other category.
   - Keep only US option-listed exchanges (`NASDAQ`, `NYSE`, `AMEX`/`NYSEARCA`/`ARCA`/`BATS`/`CBOE`); drop LSE / OTC / Stockholm / XETR rows (e.g. `BESIY`, `IQE.L`, `SIVE.ST`).
   - De-duplicate symbols, keeping the highest-priority category (`core` > `satellite` > `watch1`).
   - `--symbols` may only **narrow** this universe; tickers outside core/satellite/watch1 are ignored. Supabase credentials are mandatory (the runner exits if they are missing).
   - Implemented in `build_watchlist_universe()`; select only `symbol`, `category`, `exchange` (never `position` or `hedge`).
1. **100% Stateless on Portfolio Holdings:**
   - The runner has **zero visibility into brokerage holdings or `watchlist.position`**.
   - Never gate `WHEEL_CSP` or `WHEEL_CC` on share ownership or prior `ASSIGNED` status. Every symbol is evaluated purely on market structure, Unusual Whales data, J Law levels, GEX walls, and Dark Pool accumulation/distribution.
2. **Strict Unusual Whales API Compliance (`unusual-whales-api` contract):**
   - **Base URL:** Always `https://api.unusualwhales.com` (never `/api/v1/` or `/api/v2/`).
   - **Headers:** Always pass `Authorization: Bearer <UW_API_KEY>` and `UW-CLIENT-API-ID: 100001`.
   - **Method:** `GET` only.
   - **Whitelisted Endpoints Used:**
     - `/api/stock/{ticker}/earnings` (TTM EPS, YoY EPS growth, earnings surprise %, upcoming earnings blackout)
     - `/api/stock/{ticker}/financials` (fundamental statements supplement)
     - `/api/stock/{ticker}/interpolated-iv` (7d/30d/60d/90d IV, IV Rank/Percentile, term-structure ratios)
     - `/api/stock/{ticker}/technical-indicator/RSI` (`interval=daily&time_period=14&series_type=close`)
     - `/api/stock/{ticker}/option-contracts` (`exclude_zero_vol_chains=false`, NBBO bid/ask, delta, IV, OI)
     - `/api/option-trades` (`ticker_symbol={ticker}`, strike-level ask-side sweep veto and institutional bid-side put writing confirmation)
3. **Unusual Whales Replaces Retired `equity_valuation_signal_daily`:**
   - Do **not** query `equity_valuation_signal_daily`.
   - Compute `uw_ttm_eps`, `uw_pe_ratio`, `uw_eps_growth_yoy_pct`, `uw_peg_ratio`, `uw_latest_surprise_pct`, and `uw_valuation_tier` (`cheap`, `fair_low`, `fair_high`, `expensive`, `not_meaningful`) deterministically via `compute_uw_valuation_context()` in `scripts/uw_options_income_runner.py`.

---

## Stateless Strategy Routing Matrix

| Strategy | Bucket | Target DTE | Delta Band | Regime & Fundamental Triggers | Structural Floor / Ceiling & Economics Gate |
|---|---|---:|---|---|---|
| `WHEEL_CSP` | `CORE` | 25–39d (~32d) | `0.18 <= \|Δ\| <= 0.28` | - US-listed `core`, `satellite`, `watch1`<br>- J Law `Valid` or `Watch` (`stop_loss > 0`)<br>- `verdict` in `{"bullish", "mixed"}`<br>- `wheel_fundamental_ok == True` (`uw_ttm_eps > 0`, `uw_valuation_tier` in `{"cheap", "fair_low", "fair_high"}`)<br>- `uw_iv_rank >= 25%`, `uw_term_ratio_7_30 <= 1.25`<br>- `32 <= uw_rsi_14 <= 60` (Davis pullback rule)<br>- `dp_accumulating != False` | - `short_strike <= safety_floor` (`min(jlaw_stop_loss, gex_put_wall, dp_top_level)`)<br>- `annualized_pct >= 20%` at `short_bid`<br>- `strike_adverse_sweeps == 0`<br>- Earnings clear past expiry |
| `WHEEL_CSP` | `TACTICAL` | 3–10d | `0.06 <= \|Δ\| < 0.20` | - Same as `CORE`, plus `uw_term_ratio_7_30 <= 1.18` | - `short_strike <= safety_floor`<br>- `annualized_pct >= 18%`<br>- `strike_adverse_sweeps == 0` |
| `WHEEL_CC` | `CORE` | 14–35d | `0.16 <= Δ <= 0.28` | - Stateless covered-call reference board for `core` & `satellite`<br>- `uw_rsi_14 >= 60`, OR `covered-call` in verdict reasons, OR `verdict` in `{"mixed", "bearish"}`<br>- `uw_iv_rank >= 20%` | - `short_strike >= safety_ceiling` (`max(gex_call_wall, jlaw_target_price)` or `spot * 1.04`)<br>- `annualized_pct >= 12%`<br>- `strike_adverse_sweeps == 0`<br>- Earnings clear past expiry |
| `BULL_PUT_SPREAD` | `CORE` | 14–35d | `0.18 <= \|Δ_short\| <= 0.28` | - J Law `Valid` or `Watch` (`jlaw_score >= 8`)<br>- `verdict` in `{"bullish", "mixed"}`<br>- `uw_iv_rank >= 30%`, `dp_accumulating != False` | - `short_strike <= safety_floor`<br>- Width = **$5** (`spot < $150`) or **$10** (`spot >= $150`)<br>- Strict NBBO `0.20 <= return_on_risk <= 0.35` (`(short_bid - long_ask) / max_loss`)<br>- `strike_adverse_sweeps == 0` |
| `SHORT_STRANGLE` | `STRANGLE` | 28–45d | `0.12 <= \|Δ_put\|, Δ_call <= 0.20` | - **Positive GEX Pin:** `gex_regime == "positive"`<br>- **High IV:** `uw_iv_rank >= 45%`<br>- **Range-Bound:** `38 <= uw_rsi_14 <= 62` & `verdict` in `{"mixed", "no-data"}`<br>- Corridor `(gex_call_wall - gex_put_wall) / spot >= 8%` | - `short_put_strike <= gex_put_wall` (and `<= dp_top_level` if accumulating)<br>- `short_call_strike >= gex_call_wall`<br>- `annualized_pct >= 18%` on 20% BPR approximation<br>- Zero adverse sweeps on both legs |
| `DIAGONAL_SPREAD` | `DIAGONAL` | Back: 60–95d<br>Front: 7–21d | Back: `0.70–0.82Δ`<br>Front: `0.20–0.30Δ` | - **Low-IV Bullish Breakout:** J Law `Valid` (or `Watch` with `jlaw_score >= 11`)<br>- `verdict == "bullish"` & `dp_accumulating == True`<br>- `uw_iv_rank < 38%` & `uw_term_ratio_30_60 <= 1.08` | - Back ITM call extrinsic `<= 25%` of `long_ask`<br>- Front OTM call `>= safety_ceiling`<br>- **No Upside Inversion Gate:** `entry_debit <= 0.78 * (short_strike - long_strike)`<br>- `strike_adverse_sweeps == 0` |
| `BEAR_CALL_SPREAD` | `CORE` | 14–35d | `0.18 <= Δ_short <= 0.28` | - `verdict == "bearish"` & (`bearish_z >= 1.0` or `dp_accumulating == False`)<br>- `uw_iv_rank >= 30%` | - `short_strike >= safety_ceiling`<br>- Width = **$5** (`spot < $150`) or **$10** (`spot >= $150`)<br>- `return_on_risk >= 0.20` |

---

## Execution Workflow

### Step 1: Supabase Destination Table (LIVE)
The destination table `public."uw-option-opportunities"` **already exists** in the Supabase project `vmxxmdtzvwizrpjrrvnp` (migration `create_uw_option_opportunities` applied 2026-10-21): 100+ columns covering strategy metadata, short/leg2 option quotes, structure economics, UW fundamentals/IV/RSI, upstream JLaw/flow/GEX/dark-pool signals, the composite score, and the hypothetical lifecycle checkpoints.

- **RLS is enabled** with no anon/authenticated policies; only the service-role key (as used by the runner) can read/write.
- A `BEFORE UPDATE` trigger (`uw_opt_opp_updated_at_trg`) auto-touches `updated_at` on every PATCH.
- Idempotent **upsert by `opportunity_key`** (`report_date|strategy|dte_bucket|symbol|short_expiry|short_strike|second_strike`): the runner first selects the existing row, preserves settled terminal fields (`status`, `outcome`, `win`, `profit50_hit_date`, `dte21_mark_date`, `exit_date`) when `outcome`/`win` are already set, then PATCHes; otherwise it POSTs a new row. Same-day re-runs and lifecycle passes are therefore merge-safe.
- Re-create from scratch (fresh project only) with `sql/uw_option_opportunities.sql`, which matches the live schema including RLS and the trigger.

### Step 2: Run the Daily Stateless Scanner & Lifecycle Tracker
```bash
export UW_API_KEY="your_uw_token"
export SUPABASE_URL="https://vmxxmdtzvwizrpjrrvnp.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="your_supabase_service_key"

python3 skills/uw-options-income-engine/scripts/uw_options_income_runner.py \
  --date "$(date +%F)" \
  --min-score 60 \
  --top-n 20 \
  --output-dir reports
```

- Use `--symbols NVDA,AVGO,META,TSM` to test a subset (only tickers already in core/satellite/watch1 are scanned).
- Use `--dry-run` to generate `reports/uw_options_income_<date>.md` and `.json` without writing to Supabase.
- Use `--skip-tracking` to skip the open-ledger lifecycle evaluation pass.

### Step 3: Run Offline Unit Tests
```bash
python3 skills/uw-options-income-engine/tests/test_uw_options_income_runner.py
```

---

## Files Included in This Skill

- `SKILL.md` — Operational instructions and routing specification.
- `references/methodology.md` — Detailed valuation formulas, 7-pillar 0–100 scoring breakdown, and lifecycle rules.
- `scripts/uw_options_income_runner.py` — Deterministic Python runner and CLI.
- `scripts/requirements.txt` — Python dependency (`requests>=2.31.0`).
- `sql/uw_option_opportunities.sql` — Idempotent Supabase table DDL for `public."uw-option-opportunities"`.
- `tests/test_uw_options_income_runner.py` — Offline test suite covering all strategies, UW valuation replacement, sweep vetoes, and lifecycle rules.
