---
name: stldc-reversal-pipeline
description: Daily reversal alert-and-confirmation pipeline for the core+satellite watchlist. Runs the Supertrend + Lorentzian Classification ladder (SMA20-loss / trend-break alerts, ST + LDC confirmations, SPY + paired-sector-ETF overlays) on closed daily bars and upserts state plus 2H entry-timing events to Supabase (trade-stldc-v2 / trade-stldc-v2-events). Standalone — no dependency on jlaw pipelines. Use for operating, re-running, debugging, or extending the daily reversal pipeline.
icon: radar
color: Blue
---

# st+LDC Reversal Pipeline

Purpose: **alert and confirm trend reversals** (bull → bear and bear → bull) on
the daily chart — not an entry system. Every stage has measured precision from
a 5-year validation across the watchlist (numbers below).

Research tooling only. No orders, no brokerage connection, no personalized
financial advice.

## The ladder (daily bars, last closed candle only)

| Side | Stage | Rule |
|---|---|---|
| BEAR | **BEAR-ALERT** | close crosses below SMA20 while still above SMA50 |
| BEAR | **BEAR-CONFIRM** | within 10 sessions of the alert: trend-break (close < SMA20 < SMA50) **or** Supertrend(10, ×3) bear flip |
| BULL | **BULL-ALERT** | close crosses back above SMA20 while still below SMA50 |
| BULL | **BULL-CONFIRM** | within 10 sessions: bull stack (close > SMA20 > SMA50) **or** Supertrend bull flip |

**LDC role (corrected labels — `src[4] < src[0] → short`, the Pine convention):**
exhaustion/timing tag only. An LDC flip within 5 sessions of an alert is
recorded; LDC short flips near bear alerts are a *fade* tag, LDC long flips
near bull alerts accelerate confirmation.

**Context tags (reported, not gating):** SPY regime (close vs SMA20 vs SMA50)
and the paired best-1y-correlation sector ETF regime.

## Validation (5y, 66 tickers, events measured over a 20-day horizon)

| Stage | n | Precision | Median move |
|---|---:|---:|---:|
| BEAR-ALERT alone | 2,101 | 45.2% (base 44.2%) | +1.6% |
| **BEAR-CONFIRMED ≤10d** | 847 | **64.2% lower** | **−4.0%** |
| BULL-ALERT alone | 1,763 | 52.0% (base 55.8%) | +0.8% |
| **BULL-CONFIRMED ≤10d** | 822 | **70.4% higher** | **+8.4%** |

Freshness rule: alerts while SPY/the paired ETF has **not** already rolled are
the reliable ones (late alerts tested 30–33% precision — fade them). Standing
finding: Supertrend bearish flips and LDC directional states carry no
standalone edge; the edge is in the alert→confirm ladder and the MA structure.

## Chain

```
Supabase watchlist (core+satellite)
        │
        ▼
scripts/stldc_supabase_runner.py
  ├── ldc_engine.py          Lorentzian Classification v2.0 port + Supertrend
  ├── ldc_st_runner.py       yfinance fetch helpers (closed-bar filtering)
  ├── state ladder → upsert trade-stldc-v2        (run_date × symbol, merge-duplicates)
  └── daily confirms → 2H trigger variants → upsert trade-stldc-v2-events
        (st2h / macd_x / rsi_50 / sma_x / macd_sma / st_rsi / macd_rsi60 /
         macd_vol / first_any / baseline; ret_4/12/20 outcomes mature over ~7 sessions)
```

## Supabase tables

* **`trade-stldc-v2`** — per-bar state history (grain: `run_date` × `symbol`).
  Key fields: `stage`, `stage_date`, `confirm_kind`, `confirm_lead`, component
  scores (`score_structure/supertrend/ldc`, sum = `score`), engine values
  (SMA20/50/200, RSI14, MACD hist, ST dir/line, LDC signal/prediction),
  overlays (`spy_regime`, `pair_etf`, `pair_etf_regime`), `ldc_short_tag` /
  `ldc_long_tag`.
* **`trade-stldc-v2-events`** — 2H entry-timing events (grain: run_date =
  daily-confirm date × symbol × direction × trigger_kind × trigger_ts).
  `outcome_status` starts `pending` and flips `matured` once ret_4/12/20 are
  computed on later runs (20-bar 2H horizon ≈ 7 sessions).

Both are service-role-write only; upserts use PostgREST
`Prefer: resolution=merge-duplicates` and are idempotent.

## Daily operation

```bash
# after each US close (e.g. weekday 11:00 UTC+8 = post-close)
python scripts/stldc_supabase_runner.py

# variants
python scripts/stldc_supabase_runner.py --state-only          # skip the 2H events layer
python scripts/stldc_supabase_runner.py --seed-days 2000      # full-history 2H event backfill
python scripts/stldc_supabase_runner.py --state-history 1900  # per-bar state backfill (full 5y)
```

Re-runs on the same day merge; they never duplicate.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install yfinance "pandas==2.2.3" "numpy<2" requests tzdata "setuptools<81" pandas_ta
# pandas_ta 0.3.14b0 is no longer on PyPI — install from conda-forge tarball:
curl -L https://conda.anaconda.org/conda-forge/noarch/pandas-ta-0.3.14b-pyhd8ed1ab_0.tar.bz2 -o pt.tar.bz2
tar -xjf pt.tar.bz2 -C .venv/Lib/site-packages --strip-components=1 site-packages && rm pt.tar.bz2
```

Credentials: `SUPABASE_URL` / `SUPABASE_KEY` (service role) environment
variables, else the legacy `ai-equity-trader-v2/.env` fallback. The paired-ETF
mapping is read from `out/watchlist_etf_pairs.csv` (produced by
`etf_pairing_study.py`); if absent the pipeline falls back to SPY context for
all symbols with a logged warning. Note pandas_ta returns `None` (not NaN) for
SMA200 when a ticker has <200 sessions — the runner guards this.

## QC hooks (jlaw-v2-check parity)

1. Coverage — state rows per `run_date` vs the `watchlist` table.
2. Score-sum invariant — `score = score_structure + score_supertrend + score_ldc`.
3. Stale-pending — events in `pending` beyond ~10 sessions need a backfill.
4. Cross-system conflict — any `BEAR-CONFIRM` symbol with a jlaw Valid/A+ on
   the same date is a defect alarm (currently zero conflicts by construction).
