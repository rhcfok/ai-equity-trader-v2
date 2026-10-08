---
name: quant2-leaderboard-pipeline
description: Daily pipeline that runs the quant2 technical-direction-probability backtest (from the rhcfok/quant2 repo), validates it, builds the leaderboard, upserts results to Supabase, publishes a dated leaderboard sub-page to Notion, and refreshes a Kimi Work dashboard widget. Use for operating, re-running, or modifying the daily quant2 leaderboard workflow.
---

# quant2 Leaderboard Pipeline

Publishes the quant2 (technical-direction-probability) study's daily leaderboard
to three surfaces: **Supabase** (system of record), **Notion** (dated archive
pages), and a **Kimi Work Canvas widget** (live dashboard).

Upstream study: `github.com/rhcfok/quant2` — 18 predeclared daily technical
conditions vs 7-trading-session up-close probability, 102-ticker US watchlist,
walk-forward selection, untouched 2-year holdout, 8 validation gates. Honest
negative results ("No minimal technical strategy validated in this sample.")
are first-class outcomes; `probability_status = "not_validated"` is expected.

## Chain

```
quant2 backtest (Yahoo v8 OHLCV fetch + walk-forward + holdout gates)
  -> validate (10 independent integrity checks, must be OVERALL: PASS)
  -> make_leaderboard (leaderboard_all.csv + mae_strike_bridge.csv)
  -> upsert Supabase (quant2_runs / quant2_rule_scorecard /
                      quant2_leaderboard / quant2_mae_strike_bridge)
  -> Notion child page "quant2 Leaderboard — <as_of>" under ai-trader-v2-kimi/quant2
  -> Kimi Work widget refresh (code automation -> binding -> Canvas widget)
```

## Scripts

- `scripts/quant2_upsert_supabase.py` — upserts one completed run directory
  into the four Supabase tables via PostgREST (`Prefer: merge-duplicates`;
  idempotent, keyed by `run_name`). Credentials from `.env`
  (`supabase_url`, `supabase_service_role`) next to the script.
  Supports `--dry-run`.
- `scripts/quant2_schema.sql` — DDL for the four tables (run once in the
  Supabase SQL editor or as a migration).
- `scripts/fetch_quant2_leaderboard.py` — Kimi Work code-automation entry
  (`run(ctx)`): reads the latest run from Supabase and emits the widget
  artifact (rows + group summary). Credentials via automation input
  (`x-local`/`x-secret`), with a `.env` fallback (`QUANT2_ENV_FILE`).

## Daily operation

```bash
RUN=runs/run_$(date +%Y%m%d)
python skills/technical-direction-probability/scripts/run_direction_probability.py \
    backtest --config configs/watchlist_config.yaml --output-dir "$RUN"
python skills/technical-direction-probability/scripts/run_direction_probability.py \
    validate --run-dir "$RUN"           # must print OVERALL: PASS
python skills/technical-direction-probability/scripts/make_leaderboard.py \
    --run-dir "$RUN" --out-dir "$RUN/leaderboard"
python quant2_upsert_supabase.py --run-dir "$RUN"
```

Fetch+compute takes ~5–10 min for 102 tickers; per-ticker caching makes a
timed-out run resumable (re-run; add `--no-fetch` once all 102 `_raw.csv`
files exist).

In Kimi Work this chain runs as the **quant2 daily pipeline** automation
(weekdays 16:23 ET, America/New_York): backtest → validate → leaderboard →
Supabase → Notion page → widget refresh, then a summary reply. A separate
**quant2 leaderboard refresh** code automation (weekdays 17:13 ET) re-reads
Supabase into the Canvas widget as a fallback.

## Surface conventions

- **Supabase** tables are keyed by `run_name` (= run directory name,
  `run_YYYYMMDD`); re-running the same day merges, never duplicates.
- **Notion** parent page `ai-trader-v2-kimi/quant2` holds one child page per
  trading day titled `quant2 Leaderboard — <as_of>` (icon 📈), each with a
  summary line, day notes, a Top-25 table, and a group-summary table —
  same pattern as the sibling `jlaw & option breakout` page. Skip creation
  if a page for that as-of date already exists.
- **Widget** shows rank/ticker/group/score bar/RSI/active-rule chips plus
  group cards; oversold rows highlighted. The score is always labelled a
  historical frequency, never a calibrated probability.

## Critical rules

1. Never upsert or publish a run whose `validate` step is not `OVERALL: PASS`.
2. Never present `score_hist_freq` as a probability; model governance in the
   upstream repo owns probability vocabulary.
3. No credentials, API keys, or `.env` files in this skill — scripts read
   environment/`.env` at runtime only.
4. Close every report with:
   `This is research and analysis only, not personalized financial advice.`
