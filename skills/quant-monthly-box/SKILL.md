---
name: quant-monthly-box
description: Backtest and govern monthly price-box strategies across the live US watchlist stored in Supabase. Use before proposing, validating, or integrating prior-month breakout, five-day initial-balance, or monthly-open reclaim rules with Quant2.
---

# Quant Monthly Box

Run this skill as an **incubation backtest**, not a production signal generator. It reads the current Supabase `watchlist`, accepts only explicit US listings, and preserves the category order `core`, `satellite`, `watch1`.

## Decision

Keep the strategy **standalone during research**. Quant2 evaluates predeclared daily technical conditions over a seven-session horizon with its own walk-forward and holdout governance; monthly-box rules are trade systems with month-based entries and exits. Do not append their signals to `quant2_leaderboard` or change Quant2 scoring before every induction gate passes. Read [references/quant2_integration.md](references/quant2_integration.md) before proposing an integration.

## Rules under test

The runner tests only these predeclared long-only rules:

1. **Prior-month high breakout:** a close exceeds the previous completed month’s high, with `SMA50 > SMA200` and volume at least its 20-day average.
2. **Five-day initial-balance breakout:** after the first five sessions of the month, a close exceeds their high, with the same trend and volume filters.
3. **Monthly-open reclaim:** price first trades at least one prior-day `ATR20` below the month open, then closes back above the month open, with the same filters.

Signals are evaluated at the close and filled at the next session’s open. A current month’s final high/low is never used as a current-month signal. Yahoo’s `adjclose/close` ratio is applied consistently to OHLC so split and ex-dividend discontinuities cannot create artificial signals; every material factor change is separately audited.

## Run

Install the only runtime dependencies if needed:

```bash
python3 -m pip install pandas numpy
```

Set `SUPABASE_URL` and either `SUPABASE_KEY` or `SUPABASE_SERVICE_ROLE_KEY`. Do not put credentials in the repository. Then run:

```bash
python3 skills/quant-monthly-box/scripts/run_monthly_box_backtest.py \
  --output-dir runs/monthly_box_$(date -u +%Y%m%d) \
  --years 10 --holdout-years 2 --round-trip-cost-bps 20 --max-workers 4 \
  --run-operator-id "runner-agent-unique-id"
```

The runner caches raw Yahoo v8 daily OHLCV in `output-dir/raw/`; re-run with `--no-fetch` only after confirming all expected cache files are present. It rejects stale, short, malformed, or materially gapped cache files; an isolated missing weekday is recorded in `data_gaps.csv` for holiday/source review, while two or more consecutive missing weekdays fail the run. It fails rather than truncating the US universe and requires all three categories. Use `--watchlist-file` only for a reviewed CSV/JSON snapshot with `symbol`, `exchange`, and `category` columns.

The 10-year / two-year / 20-bps / 2,000-bootstrap-draw defaults are the only configuration eligible for an induction review. Use `--exploratory` only for sensitivity research: it permanently sets that run’s verdict to `DO NOT INDUCT`.

## Required review before induction

A **separate AI agent** must review the completed run, rather than trusting the agent that ran it. Give that agent the generated `report.md`, `validation.json`, `scorecard.csv`, `category_results.csv`, `ticker_results.csv`, `trades.csv`, `boundary_purged_trades.csv`, `corporate_actions.csv`, and `manifest.json`. Require it to:

1. confirm the watchlist is US-only and covers `core`, `satellite`, and `watch1`;
2. verify all entries occur strictly after their signal and at the next open;
3. check holdout results independently of development results;
4. challenge costs, ticker concentration, category concentration, and corporate-action gaps;
5. issue either **DO NOT INDUCT** or a documented, reproducible induction recommendation.

Use [references/independent_review.md](references/independent_review.md) as the exact review brief. Every run must use a unique nonblank, no-whitespace `--run-operator-id`. To close the separate-review gate, save the reviewer’s JSON attestation with `run_name`, `code_sha256`, `config_sha256`, the matching `run_operator_id`, a different nonblank/no-whitespace `reviewer_agent_id`, `reviewed_utc`, and `verdict`, then re-run with `--no-fetch --independent-review-file path/to/review.json`. The runner accepts the attestation only if its full configuration—including exploratory status and raw-universe hashes—matches, and the declared reviewer identity differs from the running-agent identity.

## Induction gate

Treat `validation.json` as a gate, not a ranking. A strategy is eligible for a governance review only when all of these hold on the untouched two-year holdout:

- enough observations (`>= 50` trades across `>= 10` tickers);
- positive mean and median **net** return after 20 bps round-trip cost;
- positive lower bound of the ticker-cluster bootstrap 95% CI;
- positive development mean return without tuning on the holdout;
- at least 10 holdout trades and a positive mean return in **each** of `core`, `satellite`, and `watch1`;
- positive mean return at 40 bps round-trip cost;
- no single ticker contributes more than 25% of absolute holdout P&L;
- no category contributes more than 60% of absolute holdout P&L;
- positive mean return in both halves of the holdout; and
- complete valid history for every accepted ticker (short/delisted/problematic histories are reported but prevent induction).

The runner also purges any trade whose signal, entry, or exit crosses the first full holdout month; checks the fixed configuration; and requires a matching independent-review attestation. A current live watchlist is **not** a point-in-time historical universe, so the delivered runner intentionally keeps its verdict at `DO NOT INDUCT` until a dated historical-universe source is provided and reviewed.

A failing or incomplete run is an honest research outcome. Do not publish it to Quant2 or treat it as an actionable signal.

## Outputs

| File | Purpose |
|---|---|
| `manifest.json` | Run parameters, source timestamps, accepted/rejected watchlist rows, fetch failures |
| `trades.csv` | Trade-level audit trail with signal, next-open entry, exit, and net/gross return |
| `boundary_purged_trades.csv` | Trades excluded because they touch the development/holdout boundary |
| `corporate_actions.csv` | Material Yahoo adjustment-factor changes requiring broker-quality verification |
| `data_gaps.csv` | Isolated permitted weekday data gaps; multi-day gaps fail the run |
| `ticker_results.csv` | Per-ticker results by strategy and sample |
| `category_results.csv` | Core/satellite/watch1 aggregates by strategy and sample |
| `scorecard.csv` | Development, holdout, cost-sensitivity, and induction-gate results |
| `validation.json` | Machine-readable gate verdicts and explanations |
| `report.md` | Human review report; closes with the required research disclaimer |

## Quality checks

Run the deterministic test suite before asking a separate agent to run the full study:

```bash
python3 skills/quant-monthly-box/scripts/test_monthly_box_backtest.py
```

Do not add new rules, change thresholds, alter the categories, or modify Quant2 tables during this research phase.
