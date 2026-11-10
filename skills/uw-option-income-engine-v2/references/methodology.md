# UW Option Income 2 — Methodology & Data Contract

Read before modifying wall computation, strategy gates, quota policy, scoring, or lifecycle.

## 1. Design Principles

1. **Short-term credit only:** 4 strategies (`SHORT_PUT`, `SHORT_CALL`, `BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`), two DTE buckets — `LT8` (1–7) and `DTE8_14` (8–14). No strangles, diagonals, or 25–45 DTE wheel structures (those stay in v1).
2. **Walls are term-structure aware:** a DTE-7 put wall can sit much nearer spot than the DTE-14 wall. Every strike anchor uses the wall computed **inside the bucket's expiry window**; the Supabase `gex_snapshot` wall (0–30 DTE blend) is fallback only and flagged `WALL_GEX_BLEND_FALLBACK`.
3. **Stateless:** no brokerage holdings; SHORT_CALL is an undefined-risk *reference* row, capped at 4 planned contracts/week.
4. **Diversification is a hard planner constraint, not a preference:** 10 tickers × 2 contracts, ≤2 tickers/sector, both buckets represented, 40:60 single-leg:spread.
5. **Credit realism:** spreads must clear **strict NBBO credit > $0.70/share** ($70/contract); the $5,000/week target is reported as a projection with an honest gap, never back-solved by loosening gates.

## 2. Bucket Wall Computation (`compute_bucket_walls`)

Mirrors the `uw-gex` skill methodology, restricted to the bucket window:

1. Keep contracts with `bucket_lo <= dte <= bucket_hi`, `open_interest > 0`, `gamma > 0`.
2. Per side (put/call), keep only the **nearest-DTE expiry per strike** inside the bucket (a strike listed at both 6 and 13 DTE contributes its 6-DTE row to LT8 and its 13-DTE row to DTE8_14).
3. Per-strike magnitude `gamma × OI × 100 × spot × 0.01 / 1e6` ($MM per 1% move; unsigned, dealer-position-free by design, matching `uw-gex`).
4. `call_wall` = max-magnitude call strike **at/above spot**; `put_wall` = max-magnitude put strike **at/below spot**.

Fallback chain: bucket chain wall → `gex_snapshot` blended wall (`wall_source='gex_blend'`) → skip the strategy (walls are mandatory). A one-sided chain wall mixes sources and is marked `bucket_chain+blend`.

## 3. Strategy Gates

See the routing matrix in `SKILL.md`. Additional detail:

- **Put anchor** = `min(bucket_put_wall, jlaw_stop_loss)` when the J Law stop is valid and below spot; wall alone otherwise. **Call anchor** = `max(bucket_call_wall, spot × 1.03)` (SHORT_CALL) or `bucket_call_wall` (BCS short leg).
- **Spread width by sector group (2026-11-08):** tech sectors (`TECH_SECTORS`) prefer **$5-wide** verticals (`SPREAD_WIDTH_PREFERENCE = (5, 10)`); all other sectors prefer **$10-wide** (`SPREAD_WIDTH_PREFERENCE_NONTECH = (10, 5)`) — low-vol staples/pharma names produce $0.15–$0.50 on a $5-wide at ≤14 DTE and would never clear the credit gate. Fallback to the other width when the preferred one yields no qualifying spread. The RoR floor scales with width (`ROR_FLOOR_BY_WIDTH`: 0.15 on $5, 0.10 on $10); the $0.70 absolute credit gate is unchanged (7% of width on a $10-wide).
- **Spread credit** = `short_bid − long_ask` (strict NBBO), must be **> $0.70**; equality fails. RoR band 0.15–0.50 (BPS) / ≥ 0.15 (BCS) keeps short-DTE verticals from being lottery tickets or giveaways.
- **Single legs:** Δ 0.05–0.20, bid ≥ $0.30, **spot ≤ $600** (no naked legs on high-priced tickers — collateral and gap risk), annualized ≥ 25% (LT8) / 20% (DTE8_14) on strike (put) or 20%-of-notional BPR (call).
- **Earnings blackout:** earnings strictly after expiry; only a `postmarket` print on expiry day clears.
- **Sweep veto:** any aggressive ask-side print within ±3% of the candidate strike (`size > OI`, sweep flags, or ≥ $100k premium) kills the strike (v1 `evaluate_strike_flow_prints`).
- **Sector vetoes:** bull side blocked when sector mean RSI < 40 or mean flow ≤ −30; bear side blocked when mean RSI > 62 or mean flow ≥ 30; `Unknown` sector exempt.

## 4. Market Regime

`classify_market_regime()` maps the run-level `trade-jlaw-v2.regime` note: Risk-On / Risk-Off (incl. "distribution", "defensive") / Chop ("chop", "range", "sideways", "volatile") / UNKNOWN. Regime only tilts the **planner** (stored `plan_score_adjusted`); it never edits the deterministic `confidence_score` and never overrides a hard gate.

## 5. Confidence Score (0–100, 7 pillars)

1. **Structural Anchor & Stop Buffer (0–20):** J Law classification/score + bucket-wall compliance (+4 chain-source wall, +2 blend).
2. **Combined Unusual Flow (0–20):** v1 fused composite mapped by side; −3 conflicts, +2 whale bids, −8 sweeps.
3. **Bucket Wall & Corridor (0–15):** GEX regime, chain-source wall bonus, DTE-in-bucket.
4. **UW Fundamental (0–15):** valuation tier, latest surprise.
5. **Volatility & Term Fit (0–10):** IV rank bands; −1.5 when `iv7/iv30 > 1.20` (front-week shock).
6. **Execution & Liquidity (0–10):** OI depth, NBBO spread %.
7. **Economics, Sector & Timing (0–10):** RoR/annualized, RSI alignment, sector-health agreement.

Bands: STRONG ≥ 85 · HIGHER ≥ 70 · MODERATE ≥ 60 · LOWER < 60 (default publish floor `--min-score 60`).

## 6. Weekly Quota Planner

Two-phase deterministic fill (ties: credit desc, key asc):

- **Collateral accounting** (per contract): SHORT_PUT = strike × 100 (cash-secured); SHORT_CALL ≈ strike × 100 × 0.20; vertical = width × 100. Total across all planned contracts ≤ **$100,000/week** (`--max-weekly-collateral`).
- **Sector-performance routing:** sector trend = bear (mean RSI < 45 or mean flow ≤ −15) / bull (RSI > 55 or flow ≥ 15) / neutral. Trend-ALIGNED picks (bear side in a bear sector, bull side in a bull sector) are **exempt from the sector cap** and take +1.5 in the fill order; contrary picks keep the ≤2 tickers/sector cap and take −1.0. Bull-side structures (SHORT_PUT / BULL_PUT_SPREAD) belong in bullish sectors; bear-side (SHORT_CALL / BEAR_CALL_SPREAD) in bearish sectors.
- **Non-tech reserve:** while fewer than 5 planned tickers come from non-tech sectors, Phase A draws from the non-tech pool first (`is_nontech_row`); the reserve stays subject to the ≤2 tickers/sector cap and all other caps, never blocks the fill, and its shortfall is reported (`nontech_reserve_met`).
- **Phase A — pick rows (1 contract each):** ≤20 tickers with *new-ticker-first* ordering, ≤5 contracts/ticker, pools 16 single-leg (SHORT_CALL ≤ 4) + 24 verticals, ≤2 tickers/sector for contrary/neutral picks (aligned + `Unknown` exempt), both-tenor guarantee, and each pick must fit the remaining collateral budget.
- **Phase B — size spread orders:** round-robin in plan-rank order up to **5 contracts per spread order** (`--max-spread-contracts`) while the 40-contract quota and collateral cap hold; single legs stay at 1 contract; unused single slots spill into the spread budget.
- Sort key per candidate: `confidence + regime_tilt` (RISK_ON: bull +1.5 / bear −1.0; RISK_OFF: bear +1.5, naked bull −1.5, bull spread −0.5; CHOP: spreads +1.0 / singles −1.0).
- Output: `plan_week` (ISO), `planned_contracts`, `plan_rank`, `plan_score_adjusted`, projected `weekly_credit` (Σ credit × 100 × contracts), gap vs $5,000, `weekly_collateral_usd` vs the cap, sector/bucket splits.

## 7. Lifecycle (`evaluate_open_opportunity_row`)

1. **50% profit auto-win:** conservative NBBO close (`ask` single leg; `short_ask − long_bid` verticals) ≤ 50% of entry credit → `CLOSED/WON`.
2. **Mid-cycle mark:** first pass at ≤ ⌈entry_DTE / 2⌉ days remaining records `midcycle_*` (replaces v1's 21-DTE Davis mark, meaningless under 15 DTE).
3. **Wall-breach defense:** current bucket wall crossing the short strike (put wall below it on put structures; call wall above it with spot ≥ strike on call structures) → `defense_trigger_date` + `BUCKET_*_WALL_BREACH`.
4. **Expiry settlement:** intrinsic value; `EXPIRED_WORTHLESS` / `WON` / `LOST`, P&L capped at width for verticals.

## 8. Sector Map Provenance

`watchlist` has no sector column and the UW profile endpoint is Advanced-tier, so sectors come from the curated `SECTOR_SEED` (source `manual`, ~160 symbols, coarse GICS-like groups) with persisted overrides in `public.symbol_sector_map`. Live runs backfill the cache; `Unknown` never blocks a pick. The seed is deliberately coarse — it powers the ≤2/sector cap and health vetoes, not fundamental analysis.

## 9. Data Contracts (summary)

- **UW REST** (see SKILL.md rule 2) — chains incl. `gamma`, NBBO, Δ, IV, OI; earnings; interpolated IV; RSI(14); option trades tape.
- **Supabase** — inputs: `watchlist` (universe), `trade-jlaw-v2` (stops/targets/score/regime), `gex_snapshot` (blend fallback), `flow_verdict_rows` + `option_flow_symbol_signals` + `darkpool_symbol_daily` (flow composite legs), `symbol_sector_map`; output: `uw-option-income2` ledger (idempotent upsert by `opportunity_key`, terminal fields preserved).
