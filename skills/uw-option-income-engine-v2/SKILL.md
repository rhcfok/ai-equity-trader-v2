---
name: uw-option-income-2
description: Run the short-term credit-only options income engine (SHORT_PUT, SHORT_CALL, BULL_PUT_SPREAD, BEAR_CALL_SPREAD across LT8 1-7 DTE and DTE8_14 8-14 DTE buckets) with DTE-term-structure GEX walls computed in-engine from the UW chain, a 10-ticker / 20-contract weekly quota (40% single-leg : 60% spreads, short call capped at 4, <=2 tickers per sector), market-regime tilt, and a $5,000/week credit projection.
---

# UW Option Income 2 (`uw-option-income-2`)

Use this skill when generating, auditing, or extending the **short-term credit income board**. It is the DTE-1–14 successor to `uw-options-income-engine` (v1): 4 credit strategies only, DTE-specific walls, sector/regime diversification, and a weekly 20-contract quota plan.

Read `references/methodology.md` before changing wall computation, routing gates, the quota policy, or scoring.

---

## Critical Architectural Rules

0. **Scan Universe = `public.watchlist`, default core / satellite / watch1** (US option-listed exchanges; `watchlist` cols `symbol,category,exchange` only — never `position` / `hedge`). `--categories` selects any comma list (or `all` for core/satellite/watch1/watch2/watch3/euro — non-US listings are dropped by the exchange filter); `--symbols` may only narrow. Note: `flow_verdict_rows` coverage is tech-heavy, so spread strategies fire mostly on core/satellite/watch1 names even on `all`.
1. **100% Stateless:** no brokerage holdings, no `watchlist.position`. SHORT_CALL rows are *undefined-risk reference* rows flagged `UNDEFINED_RISK` and capped at 4 planned contracts/week.
2. **Strict UW API contract:** base `https://api.unusualwhales.com`, headers `Authorization: Bearer <UW_API_KEY>` + `UW-CLIENT-API-ID: 100001`, GET only. Endpoints used: `/api/stock/{t}/earnings`, `/api/stock/{t}/interpolated-iv`, `/api/stock/{t}/technical-indicator/RSI`, `/api/stock/{t}/option-contracts` (chain + gamma), `/api/option-trades` (strike sweep veto). (`/api/companies/{t}/profile` is Advanced-tier — do not use.)
3. **Walls are DTE-term-structure specific:** `compute_bucket_walls()` derives put/call walls *per bucket* from the fetched chain (`gamma × OI × 100 × spot × 0.01 / 1e6`, nearest-expiry-per-strike inside the bucket). The Supabase `gex_snapshot` walls are a 0–30 DTE **blend** and are only a fallback (`wall_source='gex_blend'`). Walls are mandatory anchors — no wall ⇒ strategy skipped.
4. **Flow fusion from v1 is reused unchanged:** `compute_flow_composite()` (verdict 35% / premium-today 30% / 3-day persistence 20% / dark pool 15%, staleness + conflict flags). `strong_bear` vetoes the bull side; `strong_bull` vetoes the bear side.
5. **Supabase stack:** reads `watchlist`, `trade-jlaw-v2` (anchors + run regime), `gex_snapshot`, `flow_verdict_rows`, `option_flow_symbol_signals` (batched 10-day prefetch), `darkpool_symbol_daily`, `symbol_sector_map`; writes `public."uw-option-income2"` (live runs only).

---

## Strategy Routing Matrix (all hard gates)

| Strategy | Buckets | Short-Strike Anchor | Economics | Regime / Veto Gates |
|---|---|---|---|---|
| `SHORT_PUT` | LT8, DTE8_14 | `strike <= min(bucket_put_wall, jlaw_stop_loss when valid)` | Δ 0.05–0.20 · bid ≥ $0.30 · **spot ≤ $600** · annualized ≥ 25% (LT8) / 20% (8–14) | IVR ≥ 20 · `flow_label != strong_bear` · DP not distributing · weak-sector veto · earnings clear · 0 adverse sweeps |
| `BULL_PUT_SPREAD` | LT8, DTE8_14 | short `<= min(bucket_put_wall, jlaw_stop_loss)`, long = short − width | **credit > $0.70** strict NBBO (`short_bid − long_ask`) · Δ_short 0.10–0.28 · RoR 0.15–0.50 ($5) / 0.10–0.50 ($10) · **width $5 for tech sectors, $10 for other sectors (fallback to the other width)** | verdict ∈ {bullish, mixed} · IVR ≥ 25 · `flow_label != strong_bear` · weak-sector veto · earnings clear · 0 sweeps |
| `SHORT_CALL` *(reference)* | LT8, DTE8_14 | `strike >= max(bucket_call_wall, spot × 1.03)` | Δ 0.05–0.20 · bid ≥ $0.30 · **spot ≤ $600** · annualized (on 20% BPR) ≥ 25% / 20% | IVR ≥ 25 · RSI ≥ 60 OR bearish verdict OR bear flow · `flow_label != strong_bull` · strong-sector veto · `UNDEFINED_RISK` |
| `BEAR_CALL_SPREAD` | LT8, DTE8_14 | short `>= bucket_call_wall`, long = short + width | **credit > $0.70** strict NBBO · Δ_short 0.10–0.28 · RoR ≥ 0.15 ($5) / 0.10 ($10) · **width $5 for tech sectors, $10 for other sectors (fallback)** | (bearish verdict ∧ (flow ≤ −15 ∨ DP distributing)) ∨ `strong_bear` ∨ (RSI ≥ 65 ∧ flow ≤ 0) · IVR ≥ 25 · strong-sector veto · earnings clear · 0 sweeps |

Sector health (mean RSI(14) + mean flow of scanned sector members) blocks bull-side in weak sectors (RSI < 40 or flow ≤ −30) and bear-side in strong sectors (RSI > 62 or flow ≥ 30). `Unknown` sector never blocks.

**Sector-performance routing (trend alignment):** sector trend = `bear` (mean RSI < 45 or flow ≤ −15) / `bull` (RSI > 55 or flow ≥ 15) / `neutral`. Picks whose side matches their sector trend (bear side in a bear sector, bull side in a bull sector) get a +1.5 planner bonus and are **exempt from the ≤2 tickers/sector cap** — a bearish sector with many qualified SHORT_CALL/BCS names is exactly where those structures belong. Contrary/neutral picks keep the cap and take a −1.0 penalty.

**Sector width split & non-tech reserve (2026-11-08):** tech sectors (`TECH_SECTORS`: Semiconductor, Software & Cloud, Internet & Media, Tech Hardware, Networking Equipment, Data Center & Crypto, Fintech & Trading, ETF) trade **$5-wide verticals**; every other sector trades **$10-wide** (fallback to the other width when none qualifies). The RoR floor scales with width: 0.15 on $5-wide, 0.10 on $10-wide — wider spreads run structurally lower credit/max-loss, and low-vol staples/pharma otherwise never clear a 0.15 floor. The planner reserves **≥5 planned tickers from non-tech sectors** (reserve-first fill, still subject to the ≤2 tickers/sector cap and all other caps; shortfall reported honestly when non-tech candidates run out). The $0.70 absolute credit gate is unchanged — on a $10-wide spread it equals 7% of width.

---

## Weekly Quota (default 40 contracts, 20 tickers, $100k collateral cap)

Two-phase planner (`plan_weekly_quota()`):

- **Phase A — pick rows (1 contract each):** up to **20 tickers** (new tickers preferred over stacking), ≤5 contracts/ticker, single-leg pool = 16 (SHORT_CALL ≤ 4) : vertical pool = 24, ≤2 tickers/sector (`Unknown` exempt), both DTE buckets forced in, every pick must fit the remaining **$100,000 weekly collateral budget** (short put = strike × 100 cash-secured; short call ≈ 20% of notional; vertical = width × 100).
- **Phase B — size spread orders:** round-robin top-up in plan-rank order up to **5 contracts per spread order** while the 40-contract quota and collateral cap hold. Single legs stay at 1 contract. Unused single-leg slots spill into the spread budget.

Regime tilt (RISK_ON favors bull side, RISK_OFF favors bear side and penalizes naked legs, CHOP favors spreads) adjusts the fill order only. Reports projected credit vs the **$5,000/week** target, weekly collateral vs the cap, and honest shortfalls.

---

## Execution Workflow

### Step 1 — Destination tables (LIVE)
`public."uw-option-income2"` and `public.symbol_sector_map` already exist in project `vmxxmdtzvwizrpjrrvnp` (applied 2026-11-07 via Management API from `sql/*.sql`). RLS on, service-role only, `updated_at` trigger, idempotent upsert by `opportunity_key` preserving settled terminal fields.

### Step 2 — Run
```bash
export UW_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
python scripts/uw_option_income2_runner.py --date "$(date +%F)" --dry-run \
  --weekly-contracts 20 --weekly-credit-target 5000 --max-tickers 10 --min-score 60
```
Flags: `--symbols` (narrow universe), `--top-n`, `--skip-tracking`, `--table`, `--output-dir`. A full core/satellite/watch1 scan ≈ 1,300 UW calls (chain fetch limited to DTE ≤ 14) — allow ~10–15 min.

### Step 3 — Tests
```bash
python tests/test_uw_option_income2_runner.py   # 58 offline tests, no network
```

---

## Files

- `SKILL.md` — this operational spec.
- `references/methodology.md` — wall term structure, gates, scoring, quota policy, lifecycle.
- `scripts/uw_option_income2_runner.py` — the engine (clients, router, planner, lifecycle, report).
- `sql/uw_option_income2.sql`, `sql/symbol_sector_map.sql` — Supabase DDL (idempotent).
- `tests/test_uw_option_income2_runner.py` — offline suite (term-structure walls, credit/RoR gates, sector vetoes & non-tech reserve, planner caps, lifecycle). Run: `python tests/test_uw_option_income2_runner.py` — no network, no Supabase.
