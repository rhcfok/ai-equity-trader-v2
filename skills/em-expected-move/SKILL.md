---
name: em-expected-move
description: Run and maintain the calibrated Expected-Move (EM) win-probability model — a drift-off lognormal on 50-day realized vol, backtested on the watchlist (30d coverage 64.5% inside +/-1 sigma; 68% band = +/-1.12 sigma). Daily snapshot job upserts per-symbol sigma/EM bands to public."em-model-snapshot"; daily scoring job attaches p_win_expiry and p_profit50_first to every open proposal on the three options boards (uw-option-opportunities, option-opportunity-davis, csp-bps-opportunity) into public."em-probability"; weekly job recalibrates. Use when asked for model win probabilities on today's option proposals, expected-move bands for a ticker, or model coverage/recalibration stats.
---

# EM Expected-Move Win-Probability Model (`em-expected-move`)

Use this skill to **measure** — not gate — the winning probability of option
income proposals after the three options workflows publish them, and to
maintain the model (daily snapshot, weekly recalibration).

Read `references/methodology.md` before changing the model math, calibration
multipliers, table schemas, or promotion rules.

## What it does

```
【model snapshot】em_snapshot.py — weekdays ~11:30 (before the boards run)
    watchlist (all categories) + symbols seen on the three proposal boards
    -> incremental yfinance bar cache (DATA_DIR/em_closes.pkl)
    -> per-symbol close, sigma(50), calib_cov_30d
    -> upsert public."em-model-snapshot" (PK symbol+as_of, idempotent)

【win-probability scoring】em_score.py — weekdays ~13:15 (after the boards)
    uw-option-opportunities  (status = PROPOSED)
    option-opportunity-davis (status = PROPOSED)
    csp-bps-opportunity      (candidate_status = ELIGIBLE)
    each table's LATEST report_date -> join snapshot -> per proposal:
      p_win_expiry       P(short leg finishes OTM), drift-off lognormal
      p_profit50_first   P(50% of credit banked before short strike touched)
      outside_band112 / calib_cov_30d / earnings_in_window flags
    -> upsert public."em-probability" (PK source_table+opportunity_key)

【recalibration】em_backtest.py --refresh — Saturdays ~09:00
    full 5y re-download + 30-day coverage backtest (drift ON and OFF)
    -> regenerates em_coverage_by_ticker.csv / em_coverage_pooled.csv /
       em_projection_{20,30}d.csv + diagnostics charts
    Monday's snapshot picks up the refreshed calib automatically
```

Scheduling lives in workspace automations ("EM model snapshot — weekdays
11:30", "EM win-probability scoring — weekdays 13:15", "EM weekly backtest
refresh — Saturdays 09:00"), timed around the three options workflows
(manus 12:00 -> davis 12:05 -> uw-income 12:30).

## Critical Rules

0. **Measure, never gate.** The model scores published proposals; it does not
   filter, reroute, or veto any workflow's output. Promotion to gating requires
   the validation loop below to pass first.
1. **One math implementation.** All probability math lives in
   `scripts/em_model.py`. Never reimplement Φ, sigma, or the two-barrier
   approximation in another file.
2. **Drift OFF is the calibrated configuration.** Backtest (30 trading days,
   5y, 10,198 non-overlapping observations): drift-OFF coverage 64.5% [63.5,
   65.4] vs the nominal 68%; drift-ON 54.3% with down-calls hitting 43.8%
   (worse than a coin flip). Any drift-on number is diagnostic only.
3. **`model_version` on every row.** Bump `em_model.MODEL_VERSION` whenever
   the math or calibration basis changes; never rewrite historical rows'
   version retroactively.
4. **Units contract:** probabilities and calib are **fractions 0–1**; credit is
   **per share**; horizons (`h_trading_days`) are **trading days**
   (`np.busday_count(report_date, expiry)`).
5. **Source tables are read-only** to this skill. Writes go to
   `em-model-snapshot` and `em-probability` only.
6. **The model is earnings-blind.** If an earnings date falls between
   report_date and expiry, both probabilities are invalid — the
   `earnings_in_window` flag marks this; the three workflows' own earnings
   gates are the primary defense.
7. **Supabase credentials** come from env vars or the repo-root `.env`
   (SUPABASE_URL / SUPABASE_KEY, service role). Never print or commit them.

## Setup

- Python: `D:/AI projects/AI-trading/.venv/Scripts/python.exe` (has pandas,
  numpy, yfinance, requests — see `scripts/requirements.txt`).
- Data dir: defaults to `<repo-root>/out/` (gitignored); override with the
  `EM_DATA_DIR` env var. Holds `em_closes.pkl` (bar cache) and the backtest
  CSV artifacts.
- Supabase tables: create once with `sql/em_model_tables.sql` (idempotent,
  RLS enabled, service-role only).

## Commands (repo root, Git Bash)

```bash
PY="D:/AI projects/AI-trading/.venv/Scripts/python.exe"

# daily snapshot (idempotent, ~1-5 min; downloads only missing symbols)
"$PY" -I skills/em-expected-move/scripts/em_snapshot.py

# daily scoring of the three boards (idempotent)
"$PY" -I skills/em-expected-move/scripts/em_score.py

# weekly recalibration (full re-download, ~5-10 min, run in background)
"$PY" -I skills/em-expected-move/scripts/em_backtest.py --refresh

# ad-hoc vertical-spread (BCS/BPS) pricing study — three-layer probabilities,
# model fair credit, breakeven ladder; works for any ticker incl. off-watchlist
"$PY" -I skills/em-expected-move/scripts/em_bcs_study.py \
    --symbol SPY --short 775 --long 785 --expiry 2026-11-20 [--credit 1.50] [--side put]

# offline tests (no network, no Supabase)
"$PY" skills/em-expected-move/tests/test_em_model.py
```

## Data Contract

`public."em-model-snapshot"` — one row per (symbol, as_of): `close`,
`sigma_daily`, `calib_cov_30d`, `calib_n`, `model_version`.

`public."em-probability"` — one row per (source_table, opportunity_key):
identifies the proposal (symbol, strategy, bucket, spot, strike, option_type,
expiry, h_trading_days, credit, delta), the two probabilities (fractions),
the flags (`outside_band112`, `earnings_in_window`), the underlying's
`calib_cov_30d`, and `model_version` + `sigma_as_of` for attribution.
Re-runs merge; nothing is ever deleted.

Field adapters per source table (name differences, status columns, and the
csp-bps per-share credit quirk — its `entry_credit` is contract dollars, use
`short_bid`) live in `ADAPTERS` in `em_score.py`.

## Validation Loop (how this earns gating rights)

Join `em-probability` against the workflows' settled lifecycle outcomes
(`uw-option-opportunities.outcome`/`win` and equivalents):

1. Bucket settled rows by `p_win_expiry` decile; compare empirical win rate.
2. Track Brier score per `model_version`; a model earns gating rights only
   if it is monotone and well-ranked across a few hundred settled rows.
3. Re-check `calib_cov_30d` coverage each weekly refresh; if pooled 30d
   drift-off coverage drifts outside ~62–70%, investigate before trusting
   any single-name probability.

## Known Limitations

- Gaussian log-returns: underestimates jumps and earnings gaps (see rule 6).
- Two-barrier `p_profit50_first` uses a linear-delta profit barrier — an
  approximation, weakest for far-OTM shorts and debit structures (diagonals
  get `p_profit50_first = NULL`).
- The weekly backtest universe is the watchlist; symbols only ever seen on
  the manus board are covered by the snapshot but have no calib history
  (`calib_cov_30d` NULL) — treat their probabilities with extra skepticism.

## Files

- `SKILL.md` — this file.
- `references/methodology.md` — formulas, backtest evidence, adapter notes.
- `scripts/em_model.py` — math + paths + creds (single source of truth).
- `scripts/em_backtest.py` — weekly recalibration CLI.
- `scripts/em_snapshot.py` — daily snapshot job.
- `scripts/em_score.py` — daily three-board scoring job.
- `scripts/em_bcs_study.py` — ad-hoc vertical-spread pricing study CLI
  (three-layer probabilities, model fair credit, breakeven ladder).
- `sql/em_model_tables.sql` — idempotent DDL for both tables.
- `tests/test_em_model.py` — offline unit tests.
- `scripts/requirements.txt` — Python dependencies.
