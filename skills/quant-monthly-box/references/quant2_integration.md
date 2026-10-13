# Quant2 Integration Decision

## Recommendation

**Incubate monthly-box strategies as a standalone research skill first. Do not integrate them into Quant2’s daily leaderboard or score until the standalone induction gates pass and an independent agent has reviewed the full study.**

## Why it is not a drop-in Quant2 feature

| Dimension | Quant2 | Monthly box | Integration consequence |
|---|---|---|---|
| Signal type | Daily technical condition | Stateful trade rule with entry/exit lifecycle | A boolean feature is not enough to represent position state or exits. |
| Horizon | Seven-session directional outcome | Remainder-of-month trade outcome | Combining the metrics would mix targets and create an incoherent score. |
| Levels | Current indicator values | Previous month high, first-five-session range, month-open anchor | Current month full high/low must not be used before month close. |
| Governance | Frozen registry, walk-forward selection, two-year holdout, integrity gates | New registry, transaction costs, trade-level execution, holdout/robustness gates | It needs a separate validation contract before any shared model governance. |
| Output | Historical frequency, explicitly not a calibrated probability | Net trade-return distribution and induction verdict | Do not label one with the vocabulary of the other. |

## Future integration path

Only after a strategy passes every standalone gate:

1. Freeze its exact rule definition, execution convention, cost model, and data version.
2. Re-run it across the same US-only categorized watchlist with a fresh untouched holdout.
3. Add a **separate monthly-box research table or artifact**; do not overload `quant2_leaderboard`.
4. Have an independent agent audit the run and issue an explicit induction recommendation.
5. If approved, expose the outcome as a distinct research overlay, such as `monthly_box_status` and `monthly_box_rule`, alongside—not inside—`score_hist_freq`.
6. Revisit a composite decision framework only if it preserves both horizons and labels Quant2’s historical frequency correctly.

## Non-negotiable guardrails

- Treat a current month’s final high/low as unknown until month end.
- Do not tune thresholds on the untouched holdout.
- Do not publish failing or incomplete research to Quant2’s production tables.
- Keep Yahoo source data, accepted US-watchlist universe, failures, transaction-cost assumptions, and run code available for audit.
- Keep the output as research and analysis only, not personalized financial advice.

## Discovered source contracts

- **Quant2 repository contract:** `skills/quant2-leaderboard-pipeline` performs a frozen-registry daily-condition study with a seven-trading-session outcome, validation gates, Supabase publication (`quant2_runs`, `quant2_rule_scorecard`, `quant2_leaderboard`, `quant2_mae_strike_bridge`), and a dashboard reader. It treats `score_hist_freq` as a historical frequency, not a calibrated probability.
- **Watchlist source:** Supabase `public.watchlist` exposes `id`, `symbol`, `exchange`, and `category`. The backtest runner must read only these fields, accept explicit US venues (`NASDAQ`, `NYSE`, `AMEX`, `ARCA`, `BATS`, `CBOE`, `IEX`, `US`, `USA`), preserve the `core` → `satellite` → `watch1` order, and never write to Supabase during research.
- **Survivorship caveat:** A live watchlist queried today is not a point-in-time historical universe. Results produced from it are exploratory and cannot support induction until a dated historical-universe source is supplied and reviewed.
