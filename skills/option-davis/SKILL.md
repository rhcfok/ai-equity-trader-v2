---
name: option-davis
description: "Run the daily Davis-playbook option opportunity workflow: read the normalized Supabase stack (flow_verdict_rows, option_flow_symbol_signals with cross-sectional z-scores, trade-jlaw-v2 option layer, gex_snapshot), price Unusual Whales option chains, publish explicit executable short-put / bull-put-spread / bear-call-spread proposals with DTE and contract names, upsert the ledger public.\"option-opportunity-davis\", and append an extendable 'Option by Davis — <date>' toggle block to Notion. Use for the scheduled weekday 15:30 run, or when asked for Davis-style option ideas, wheel/credit-spread proposals, 期权机会, or the daily option opportunity report."
---

# option-davis — Daily Option Opportunity Workflow

Wraps the Davis playbook (short puts / bull put spreads / bear call spreads) around the normalized Supabase data stack and live Unusual Whales chains. The runner is deterministic and credential-free; the only write targets are the ledger `public."option-opportunity-davis"` and (optionally) a Notion block appended by the operator.

## Prerequisites

- Python 3.10+ with `requests` (`scripts/requirements.txt`).
- Credentials via environment variables or `--dotenv` (never hardcoded):
  - `SUPABASE_URL`, `SUPABASE_KEY` (or `SUPABASE_SERVICE_ROLE_KEY`)
  - `UW_API_KEY` (or `UNUSUAL_WHALES_API_KEY`); optional `UW_BASE` (default `https://api.unusualwhales.com`)
- Upstream tables fresh from the daily pipelines: `flow_verdict_rows` (normalized verdicts), `option_flow_symbol_signals` (with z-score columns), `trade-jlaw-v2` (option layer applied), `gex_snapshot`.
- Ledger table exists: `sql/option_opportunity.sql` under `skills/UW-option-breakout/sql/` (contract: `skills/UW-option-breakout/references/option_opportunity_contract.md`).

## Run procedure

Offline unit tests first:

```bash
python3 skills/option-davis/tests/test_option_davis_runner.py
```

Daily run (ledger logging on; report written to `opportunities-<date>.md` in the working directory):

```bash
python3 skills/option-davis/scripts/option_davis_runner.py --dotenv /secure/ai.env
```

Report-only dry run (no ledger writes):

```bash
python3 skills/option-davis/scripts/option_davis_runner.py --dotenv /secure/ai.env --no-log
```

The runner auto-derives the report date (today), the ~32-DTE Friday expiry (25–39 days out, nearest to 32), and the latest available data dates from Supabase.

## Post-run: Notion block (operator step)

After a successful run, append an **extendable toggle block** to the "AI generated option idea" page (`3fcb8852-cf00-80e0-874b-e0b0407d1065`) via the Notion MCP `notion-update-page` tool (`insert_content`, position end). Notion-flavored markdown per `notion://docs/enhanced-markdown-spec` (tables are `<table>` XML, not pipe tables):

```markdown
## Option by Davis — <date> {toggle="true"}
	<indented intro line with data dates + expiry (DTE)>
	### Short puts — executable proposals
	<table fit-page-width="true" header-row="true"> ... </table>
	### Bull put spreads (5-wide)
	<table> ... </table>
	### Bear call spreads
	<table> ... </table>
	### Post-earnings watchlist
	- bullets
	### Guardrails
	- bullets
```

All children of the toggle heading must be indented one tab, or they render outside the toggle. Preserve any existing `<page>` child blocks verbatim — never delete them.

## Ledger and idempotency

One row per published opportunity, keyed by `opportunity_key = report_date|strategy|symbol|expiry|short_strike`. Merges are explicit PATCH-by-opportunity_key then INSERT (PostgREST's `merge-duplicates` upsert is not honored on this table because of the identity PK + unique constraint). Re-running a day merges; terminal `outcome`/`win` fields must never be cleared by a re-run.

## Davis rules applied (summary)

- Entry only on a red/oversold day; short strike below support (GEX put wall); 20–30 delta band (0.20–0.35 tolerance).
- DTE ≤ 45; expiry picked at ~32 DTE.
- Annualized premium filter: `credit / strike / dte * 365 >= 20%` (`davis_annualized_ok`).
- Earnings firewall: earnings strictly after expiry (`earnings_clear`); names inside the window go to the post-earnings watchlist.
- Risk 5–7% of capital per trade via buying-power reduction; decide at 21 DTE.

Full scoring weights and economics: [references/methodology.md](references/methodology.md).

## Failure handling

| Symptom | Action |
| --- | --- |
| Missing credentials | Runner exits with the missing names; provide env vars or `--dotenv`. |
| Supabase/UW network error | Built-in retries (4x backoff for reads; chains fail per symbol and are skipped). Re-run; idempotent. |
| Fewer than 5 candidates | Upstream option layer may not be applied yet; wait ~20 minutes and re-run. If still short, log a `NO_QUALIFIED_SETUP` trace row (strategy `NO_QUALIFIED_SETUP`, symbol `--`) so the day is not silently blank. |
| Ledger write failure | Runner exits non-zero with the response body; rows are merged on re-run. |
| Quote stale/wide | Legs with zero bid are marked `quote_status=stale` and the row is skipped for publishing. |
