# EM Model Methodology

Formulas, calibration evidence, and adapter notes for `em-expected-move`.
Model version namespace: `gaussian-drift-off-v1` (see SKILL.md rule 3).

## 1. Origin

The model is the analytic form of LuxAlgo's "Monte Carlo Expected Move
Distribution" Pine indicator (CC BY-NC-SA 4.0). The indicator simulates 200
GBM paths of `projLen` steps and takes p16/p50/p84 of terminal prices. Those
sampled percentiles are noisy estimates of exact lognormal quantiles, so this
skill computes the analytic distribution directly — no Monte Carlo sampling
noise in production numbers:

```
sigma   = stdev(ln(C_t / C_{t-1}), 50)          # population stdev (Pine ta.stdev)
mu      = ln(SMA50_t / SMA50_{t-1})             # OPTIONAL drift; calibrated OFF
ln S_T  ~ N(ln S0 + h*mu, (sigma*sqrt(h))^2)
```

## 2. Calibration evidence (backtest: 5y daily bars, watchlist universe,
non-overlapping anchors, h = 30 trading days unless stated)

| Universe | n | drift-OFF cov | 95% CI | drift-ON cov | 95% CI |
|---|---|---|---|---|---|
| core | 719 | 61.9% | 58.3–65.4 | 53.8% | 50.2–57.4 |
| satellite | 1,736 | 63.8% | 61.5–66.1 | 53.5% | 51.2–55.8 |
| watch1 | 3,704 | 66.1% | 64.6–67.7 | 56.2% | 54.6–57.7 |
| watch2 | 1,219 | 63.9% | 61.2–66.6 | 54.5% | 51.7–57.2 |
| watch3 | 2,780 | 63.5% | 61.7–65.3 | 52.3% | 50.4–54.2 |
| **ALL** | **10,198** | **64.5%** | **63.5–65.4** | **54.3%** | **53.3–55.2** |

At h = 20 trading days the drift-OFF coverage was 68.2% [67.2, 69.1]
(n = 9,244) — i.e. the Gaussian model is calibrated at 20d and under-covers
at 30d as fat tails dominate longer horizons.

Key derived facts baked into the model:

- **Z68 = 1.12**: the empirical 68% band at 30d is ±1.12σ, not ±1σ
  (median |z| = 0.727). Use ±1.12σ whenever a "68% band" is needed at
  ~30d horizons.
- **Breach asymmetry (drift-off, 1σ): 20.9% exit above vs 14.7% below.**
  Upside breaches are ~1.4× more likely — CC assignment risk is understated
  by nominal delta; CSP cushion is overstated. Informational, not modeled.
- **Drift (SMA-slope) hurts**: drift-ON loses ~10pp of coverage; directional
  hit rate up-calls 54.3% / down-calls 43.8% (h=30d). Hence the calibrated
  configuration is drift-OFF; `mu` exists in `em_backtest.py` only for
  diagnostics (`cov_on`, `z_on`).
- **Regime dependence**: rolling pooled coverage swings ~45–90%. Worst
  underlyings (trenders, e.g. GWW/CAT/ABBV/WDC historically) hold their band
  only 45–55% of the time; best (range-bound) hold 80–90%. This is exactly
  what `calib_cov_30d` measures per symbol — probabilities from names with
  `calib_cov_30d < 0.55` deserve a haircut, and `< 0.60` should not back a
  strangle thesis.

## 3. Win probability definitions

For a proposal with short strike K, horizon h (trading days), per-entry spot
S0, and per-share sigma:

- **p_win_expiry** = P(short leg finishes OTM at expiry):
  - put:  `Phi( ln(S0/K) / (sigma*sqrt(h)) )`
  - call: `Phi( ln(K/S0) / (sigma*sqrt(h)) )`
  For credit spreads the long leg is further OTM, so short-leg-OTM equals
  max-profit-at-expiry. This is the expiry-view "win".
- **p_profit50_first** = P(price banks 50% of the entry credit before the
  short strike is touched). Driftless two-barrier approximation in price
  space: with breach distance `d_b = |S0 − K|` and profit distance
  `d_p = 0.5 * credit / |delta|` (linear-delta move that halves the premium),
  `P ≈ d_b / (d_b + d_p)`. Assumptions: driftless arithmetic BM, constant
  delta, strike-touch = breach (matches the workflows' stop semantics).
  NULL for debit structures (diagonals) and missing delta/credit.
- **outside_band112**: True when K sits beyond the ±1.12σ band — the strike
  clears the empirical 68% band.

## 4. Adapter notes (em_score.py `ADAPTERS`)

| table | status filter | credit source | option type | notes |
|---|---|---|---|---|
| `uw-option-opportunities` | `status = PROPOSED` | `entry_credit` (per share) | OCC symbol `[-9]` char | `dte_bucket` kept as bucket |
| `option-opportunity-davis` | `status = PROPOSED` | `entry_credit` (per share) | `side` column | `SHORT_PUT`/spreads; `bucket` = dte |
| `csp-bps-opportunity` | `candidate_status = ELIGIBLE` | **`short_bid`** — the table's `entry_credit` is whole-contract dollars | OCC symbol | `bucket` = dte |

Each table is scored at **its own latest `report_date`** (boards anchor to the
last completed US session; dates can legitimately differ across tables).

## 5. Validation plan

1. Join `em-probability` to settled lifecycle outcomes by opportunity_key.
2. Decile `p_win_expiry` -> empirical win rate; require monotonicity.
3. Brier score per `model_version`; watch for regressions on model bumps.
4. Weekly: pooled 30d drift-OFF coverage must stay roughly in 62–70%;
   investigate breaches of that corridor before trusting single-name numbers.
5. Promotion rule (informational -> gating) requires: monotone deciles,
   Brier better than the delta-implied baseline, and ≥ 300 settled rows.

## 6. Provenance / license

Derived from LuxAlgo's "Monte Carlo Expected Move Distribution" Pine Script
v6 (© LuxAlgo, CC BY-NC-SA 4.0). Any redistribution of the analytic port must
carry attribution, remain non-commercial, and be share-alike. Research and
analysis only — not financial advice.
