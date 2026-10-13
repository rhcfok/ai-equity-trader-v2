# Independent Agent Review Brief

Use this brief **after** a completed monthly-box backtest. The reviewer must not modify the runner, tune parameters, or substitute data. Its job is to assess the evidence independently.

## Required inputs

- `report.md`
- `validation.json`
- `manifest.json`
- `scorecard.csv`
- `category_results.csv`
- `ticker_results.csv`
- `trades.csv`
- `boundary_purged_trades.csv`
- `corporate_actions.csv`
- `data_gaps.csv`
- the exact `run_monthly_box_backtest.py` version used

## Required checks

1. **Universe:** Confirm only explicitly US-listed watchlist rows were accepted; report accepted counts and rejections by `core`, `satellite`, and `watch1`.
2. **Data:** Confirm every accepted ticker has the stated history, record missing/short histories, review every permitted one-weekday gap in `data_gaps.csv`, and flag corporate-action or ticker-continuity caveats. A multi-weekday source gap must prevent induction.
3. **No look-ahead:** Sample trades and verify `signal_date < entry_date`, entry equals the next available session’s open, no entry crosses a calendar-month boundary, and no current-month final high/low enters a signal. Confirm the five-session initial-balance level is unavailable before its fifth session completes.
4. **Holdout separation:** Check that the untouched holdout is not used to select a strategy or thresholds. Confirm `boundary_purged_trades.csv` contains every trade touching the first full holdout month; evaluate development and holdout separately.
5. **Economics:** Challenge the 20 bps and 40 bps cost assumptions, trade count, average/median net returns, and the lower ticker-cluster bootstrap bound.
6. **Robustness:** Check category consistency, holdout-half consistency, and the largest ticker and category absolute-P&L shares. Do not accept a result driven by one ticker or one category.
7. **Corporate actions and reproducibility:** Review adjustment-factor changes, raw-cache hashes, dates, configuration hash, and code hash. Flag any item requiring broker-quality confirmation.
8. **Universe:** Treat a live watchlist as survivorship-biased unless a dated, point-in-time membership source is supplied. Without it, state `DO NOT INDUCT`.
9. **Decision:** State `DO NOT INDUCT` unless every formal gate passes, the raw files are complete, a point-in-time universe is supplied, and no material methodological flaw remains.

## Required output

Return a concise audit, then save a machine-readable attestation JSON. The attestation must contain exactly these binding fields plus optional notes:

```json
{
  "run_name": "monthly_box_YYYYMMDD",
  "code_sha256": "exact value from manifest.json",
  "config_sha256": "exact value from manifest.json",
  "run_operator_id": "exact value from manifest.json",
  "reviewer_agent_id": "different-independent-agent-identifier",
  "reviewed_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "verdict": "DO NOT INDUCT"
}
```

The runner requires a nonblank, no-whitespace `reviewer_agent_id` that differs exactly from the nonblank, no-whitespace `run_operator_id`; use platform job/agent IDs where available. The narrative must include gate-by-gate evidence, data/methodology exceptions, the exact run and fingerprints, and a recommended next action. A verdict of `INDUCTION REVIEW ELIGIBLE` is invalid without a dated point-in-time historical universe.

Never convert a positive backtest into personalized investment advice.
