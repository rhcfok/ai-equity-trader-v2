# Full Tape Provider Mapping

| Workflow input | Unusual Whales Full Tape field / rule |
| --- | --- |
| Contract identity | `option_chain_id` |
| Underlying | `underlying_symbol` |
| Expiry, call/put, strike | `expiry`, `option_type`, `strike` |
| Event/prior volume | Sum non-cancelled transaction `size` by `(underlying_symbol, option_chain_id)` |
| Event premium | Sum non-cancelled transaction `premium` by the same key |
| IV / delta / OI / spot | Last non-cancelled row by `executed_at` for a contract-day |
| Prior missing contract | Zero volume; no prior IV observation |
| Candidate DTE | `expiry - event_date` |

The Full Tape `volume` field is a running cumulative counter. Do not sum it. The runner sums `size`, which is the actual transaction quantity.

The source archive is reduced only for the selected explicitly US-listed watchlist symbols. The audit output includes source-date metrics, selected provider contract IDs, prior volumes, and the last execution used for the daily snapshot.

## Official sources

- Full Tape endpoint: <https://api.unusualwhales.com/docs/api/option-trade/full-tape.md>
- Full Tape data-lake guidance: <https://unusualwhales.com/skills/uw-options-data-lake-skill.md>
- Raw option-trade schema: <https://api.unusualwhales.com/docs/operations/PublicApi.OptionTradeController.index>
- Flow Alerts (optional enrichment only): <https://api.unusualwhales.com/docs/operations/PublicApi.OptionTradeController.flow_alerts>

Flow Alerts are not used as the baseline because they are rule-based aggregations rather than a complete contract-day tape. They may be added later as non-deterministic enrichment, but not as a replacement for the Full Tape aggregate.
