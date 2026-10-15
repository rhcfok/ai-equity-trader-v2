---
name: uw-stock-screener
description: Discover liquid U.S.-listed common-stock momentum candidates with the Unusual Whales stock screener, verify exchange listings, and prepare or apply safe watchlist intake actions. Use when running or maintaining the UW momentum discovery workflow that adds new candidates to watch1, promotes eligible lower-tier rows, and preserves watch1, satellite, and core classifications.
---

# UW Momentum Stock Screener

Use this skill to discover candidate equities and stage them for the existing watchlist workflows. This is a **discovery and intake step**, not a JLaw score, trade recommendation, or order workflow.

## Pipeline

1. Run `scripts/uw_stock_screener.py` with the configured UW and Supabase credentials. The default screen is in `references/screen-policy.md`.
2. Review the JSON audit output, especially unresolved or rejected exchange metadata and the proposed watchlist actions.
3. **Dry-run is the default.** Only pass `--apply` when the user explicitly requests changes to the live watchlist. The runner only adds/promotes rows; it does not launch downstream workflows.
4. Once approved/applied, allow the existing workflow chain to process the shared watchlist: `jlaw-v2-chart` / `jlaw-v2-yahoo`, then UW option screening, UW unusual-option flow, and Quant2/GEX as configured.

## U.S. listing and category rules

- Include only UW `Common Stock` results whose U.S. exchange is explicitly recognized. If UW omits a venue, verify it from Yahoo Finance chart metadata. Reject non-U.S., unknown, or unresolved listings; a country label by itself is insufficient proof of a U.S. exchange.
- New qualifying symbols are inserted as `watch1`.
- Keep existing `core`, `satellite`, and `watch1` rows unchanged. Promote an eligible existing lower-tier row to `watch1`; do not overwrite a protected category such as `hedge`/`hedges`.
- Deduplicate symbols case-insensitively. The watchlist table has a row ID but may not enforce a unique symbol constraint; re-read before applying and report any partial-write failure.
- Never change `remarks`, position/hedge fields, or unrelated watchlist records.

## Guardrails

- Read credentials only from process environment: `UW_API_KEY` (or `UNUSUAL_WHALES_API_KEY`), `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` (or `SUPABASE_KEY`). Never log or commit secrets.
- The runner uses UW stock/price/volume/technical fields only; it does not use option-flow fields to rank or qualify stock momentum candidates.
- The default filter is a configurable discovery heuristic, not an assertion that the UW values are the canonical JLaw score. Yahoo OHLCV remains the source for the existing deterministic JLaw score; TradingView chart review remains the structural confirmation and may downgrade a candidate.
- Do not submit orders or change brokerage settings.

## Commands

```bash
# Preview only; writes an audit JSON locally and reads the watchlist.
python3 scripts/uw_stock_screener.py --output out/uw_stock_screener.json

# Apply only after reviewing the dry-run output and receiving explicit authorization.
python3 scripts/uw_stock_screener.py --apply --output out/uw_stock_screener.json

# Check CLI options and run offline tests.
python3 scripts/uw_stock_screener.py --help
python3 -m unittest discover -s tests -v
```

## Output

The JSON audit records the screen query and timestamp, UW result count, listing-verification results, qualifying candidates, proposed/applied watchlist actions (`add`, `promote`, or `keep`), and any API or write errors. The runner does not assign a `jlaw_score` or automatically invoke any downstream workflow.
