---
name: uw-unusual-option-flow
description: "Run an IV-aware unusual-options-flow screen from Unusual Whales Full Tape archives. Use for US-listed watchlist symbols when comparing event-day contract volume and premium with the same option contract over prior trading sessions, producing audit JSON and optionally publishing compatible Supabase flow signals."
---

# Unusual Whales Full Tape Unusual Option Flow

Use this skill to replace the ORATS unusual-flow provider with Unusual Whales Full Tape while preserving the established candidate gates, three-prior-session unusualness scoring, IV-aware directional weighting, and output schema.

## Required configuration

Set secrets outside source control:

- `UW_API_KEY` or `UNUSUAL_WHALES_API_KEY` — API token entitled to the historical Full Tape endpoint.
- `SUPABASE_URL` plus `SUPABASE_KEY` or `SUPABASE_SERVICE_ROLE_KEY` — required only when reading the default watchlist or using `--write-supabase`.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `UW_BASE` | `https://api.unusualwhales.com` | API base URL |
| `UW_CLIENT_API_ID` | `100001` | Unusual Whales client attribution ID |
| `SUPABASE_WATCHLIST_TABLE` | `watchlist` | Categorized watchlist table |
| `SUPABASE_SIGNAL_TABLE` | `option_flow_symbol_signals` | Signal append table |
| `SUPABASE_POSITION_ACTIONS_TABLE` | `orats_position_actions_daily` | Position-summary backfill table |

The runner accepts a private `--dotenv` file. Never commit it.

## Core data rules

1. Accept only watchlist rows explicitly identified as US-listed (NASDAQ/NYSE/AMEX/ARCA/BATS/CBOE/IEX/US/USA). Explicit `--symbols` are treated as US ad-hoc symbols.
2. Download the Full Tape for the completed event session and each prior NYSE trading session. The runner uses a planned NYSE holiday calendar; pin `--event-date` for non-standard closures.
3. Reduce each source archive only for the selected symbols. Group by `(underlying_symbol, option_chain_id)` and **sum transaction `size` and `premium` across non-cancelled rows**. Never sum the Full Tape `volume` column because it is a running cumulative field.
4. For daily IV, delta, OI, and underlying price, retain the final non-cancelled execution snapshot for the contract. The audit file records the convention and raw contract ID.
5. Treat a contract absent from a prior-day aggregate as zero volume. This preserves the existing unusualness score.
6. Keep the existing minimums by default: volume `500`, contract premium `$1M`, unusual-evidence premium `$2M`, DTE `0–180`, and three prior sessions. Recalibrate only after a controlled dual-provider comparison.

## Run procedure

Install dependencies:

```bash
python3 -m pip install -r skills/uw-unusual-option-flow/requirements.txt
```

Run the offline test before using credentials or network:

```bash
python3 skills/uw-unusual-option-flow/scripts/uw_unusual_option_flow.py --self-test
```

Run a single-symbol, non-writing test with an explicit completed market date:

```bash
python3 skills/uw-unusual-option-flow/scripts/uw_unusual_option_flow.py \
  --dotenv /secure/uw-flow.env \
  --symbols AAPL \
  --event-date 2026-10-08 \
  --output out/uw_flow_aapl.json
```

Run the categorized US watchlist without database writes:

```bash
python3 skills/uw-unusual-option-flow/scripts/uw_unusual_option_flow.py \
  --dotenv /secure/uw-flow.env \
  --event-date 2026-10-08 \
  --output out/uw_flow_2026-10-08.json
```

Add `--write-supabase` only after reviewing the audit JSON. Writes append signal rows and patch matching `option_flow_summary` fields; repeated same-day runs can duplicate append-only signal rows unless the database has a uniqueness rule.

## Caching and storage

By default, the runner stores small reduced aggregates in `uw_unusual_option_flow_cache/aggregates/`. A cache file is reused only when it contains every requested ticker. Source ZIPs are transient by default; use `--keep-full-tape-archives` to retain them under `cache-dir/archives/` for forensic replay.

Full Tape ZIPs are large. The runner processes one date at a time and reduces it while streaming the CSV, so it does not retain the full CSV or all-market tape in memory.

## Webhook replacement

For a private deployment only:

```bash
python3 skills/uw-unusual-option-flow/scripts/uw_unusual_option_flow.py \
  --dotenv /secure/uw-flow.env --serve --host 127.0.0.1 --port 8000
```

- `POST /task3-uw-unusual-flow-jlaw` is the new endpoint.
- `POST /task3-orats-unusual-flow-jlaw` is a backward-compatible alias.
- `GET /healthz` returns service health.

Place this behind authentication and a private network/reverse proxy. Do not expose credentials through HTTP.

## Failure handling

| Symptom | Action |
| --- | --- |
| Full Tape `401` | Check the API token. |
| Full Tape `403` | Confirm historical-lookback entitlement and the requested date. |
| Archive schema failure | Keep the audit/error output, inspect new source columns, and update only after verifying field semantics. |
| No accepted US symbols | Correct `watchlist` exchange/country fields; do not include unspecified or non-US listings. |
| No candidates | This is a valid no-flow outcome; use `--emit-no-signal` if zero-result rows are required. |
| Supabase write error | Re-run without `--write-supabase`, inspect the audit artifact, then correct credentials/schema. |

See [references/methodology.md](references/methodology.md) for the provider mapping and source references.
