---
name: uw-unusual-option-flow
description: "Run two IV-aware Unusual Whales workflows for US-listed watchlist symbols: (A) unusual-options-flow screen from Full Tape archives comparing event-day contract volume and premium with the same option contract over prior trading sessions; (B) dark-pool accumulation check reducing recent dark-pool prints into buy/sell pressure, block prints, and demand levels. Both produce audit JSON and optionally publish compatible Supabase rows (flow signals, darkpool_prints, dp_* opportunity enrichment)."
---

# Unusual Whales: Unusual Option Flow + Dark Pool Accumulation

This skill covers two complementary workflows against Unusual Whales data for
the same US-listed watchlist. Workflow A replaced the ORATS unusual-flow
provider with Unusual Whales Full Tape. Workflow B adds an institutional
dark-pool accumulation check that enriches the Davis CSP/BPS opportunity
layer (`option-opportunity-davis`).

## Required configuration

Set secrets outside source control:

- `UW_API_KEY` or `UNUSUAL_WHALES_API_KEY` — API token entitled to the
  historical Full Tape endpoint and the dark-pool endpoint.
- `SUPABASE_URL` plus `SUPABASE_KEY` or `SUPABASE_SERVICE_ROLE_KEY` —
  required only when reading the default watchlist or using `--write-supabase`.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `UW_BASE` | `https://api.unusualwhales.com` | API base URL |
| `UW_CLIENT_API_ID` | `100001` | Unusual Whales client attribution ID |
| `SUPABASE_WATCHLIST_TABLE` | `watchlist` | Categorized watchlist table |
| `SUPABASE_SIGNAL_TABLE` | `option_flow_symbol_signals` | Workflow A signal append table |
| `SUPABASE_POSITION_ACTIONS_TABLE` | `orats_position_actions_daily` | Workflow A position-summary backfill table |
| `SUPABASE_DARKPOOL_TABLE` | `darkpool_prints` | Workflow B raw print table |
| `SUPABASE_OPPORTUNITY_TABLE` | `option-opportunity-davis` | Workflow B aggregate patch target |

The runners accept a private `--dotenv` file. Never commit it.

## One-time database setup (Workflow B only)

Apply `scripts/migrations/20261018_darkpool.sql` once — it creates
`darkpool_prints` and adds the `dp_*` columns to `option-opportunity-davis`.
Two equivalent ways:

```bash
# via the Management API (requires SUPABASE_ACCESS_TOKEN in env or --dotenv)
python3 skills/uw-unusual-option-flow/scripts/apply_migrations.py \
  --dotenv /secure/uw-flow.env
```

or paste the same file in the Supabase **SQL editor**. Both are idempotent;
`apply_migrations.py` records each file's SHA-256 in
`uw_unusual_option_flow_cache/migrations_applied.json` and skips already-applied
files on re-runs. Until the migration is applied, `--write-supabase` will skip
the opportunity patch and log a warning, and the raw-print insert will fail
with a table-not-found error.

---

# Workflow A — Unusual Option Flow (Full Tape)

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
python3 skills/uw-unusual-option-flow/tests/test_darkpool_fetch.py
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

---

# Workflow B — Dark Pool Accumulation

Complementary daily check that answers "are institutions actually
accumulating this wheel candidate?" before selling puts against it.

## What it does

1. Fetches the most recent dark-pool prints per symbol
   (`GET /api/darkpool/{ticker}`, ~500 prints — a rolling window covering
   roughly the latest half-day of off-exchange prints).
2. Reduces them into one accumulation summary:
   - buy/sell pressure inferred from execution versus the print-time NBBO
     midpoint (print at/above mid = buy-side),
   - block prints (`size >= --block-size`, default 10,000 shares) and their
     premium,
   - top demand level (price with the largest premium) and its size,
   - premium above/below the last print price,
   - `accumulating` = net premium > 0.
3. Writes (with `--write-supabase`):
   - every usable raw print to `darkpool_prints` with duplicate-ignore on
     `(symbol, executed_at, tracking_id)` — safe to re-run;
   - the daily summary to the `dp_*` columns of `option-opportunity-davis`
     rows for `report_date = --opportunity-date` (default today, Eastern).
     A symbol with zero opportunity rows that day is a harmless no-op patch.

## Interpretation rules (how to consume dp_*)

- **Accumulation confirmation:** `dp_accumulating = true` with meaningful
  `dp_net_mm > 0` means institutions were net buyers off-exchange — a green
  light for a cash-secured put / bull put spread on that name. A negative
  `dp_net_mm` on a "bullish" flow verdict is a distribution divergence —
  treat as a caution flag.
- **Support placement:** `dp_top_level` is an institutional demand shelf.
  Place the short put strike below it (and below the GEX put wall) per the
  Davis "strike below support" rule.
- **Conviction:** `dp_blocks` separates large single-institution orders from
  retail-sized prints; a high block count with buy-side lean is the strongest
  signal.
- **Caveats:** there is no buy/sell flag in the feed — direction is inferred
  from NBBO position. `derivative_priced` prints are excluded from the
  aggregate by default (`--include-derivative` to keep them). The 500-print
  window is a partial-day snapshot; the raw table accumulates history so
  multi-day trends can be queried later.

## Run procedure

```bash
# offline checks (no network, no credentials)
python3 skills/uw-unusual-option-flow/tests/test_darkpool_fetch.py

# live, single symbol, no database writes
python3 skills/uw-unusual-option-flow/scripts/darkpool_fetch.py \
  --dotenv /secure/uw-flow.env \
  --symbols ADI,CRM,SNOW \
  --output out/uw_darkpool_dry.json

# full watchlist, write raw prints + patch opportunities
python3 skills/uw-unusual-option-flow/scripts/darkpool_fetch.py \
  --dotenv /secure/uw-flow.env \
  --write-supabase \
  --opportunity-date 2026-10-17 \
  --output out/uw_darkpool_2026-10-17.json
```

Cached print dumps live under `uw_unusual_option_flow_cache/darkpool/`
re-keyed by symbol and opportunity date; delete a cache file to force a
refetch.

## Failure handling

| Symptom | Action |
| --- | --- |
| Dark pool `401`/`403` | Check the token entitlement for `/api/darkpool`. |
| `darkpool_prints` insert 404 | Apply `scripts/migrations/20261018_darkpool.sql` (SQL editor or `apply_migrations.py`). |
| Opportunity patch 400 | Inspect the Supabase error code in the audit; `22P02`-style type errors mean a payload/schema mismatch to fix in the runner. |
| Opportunity patch 404 | Migration not applied, or no `option-opportunity-davis` rows for that symbol/date — warning only, not an error. |
| Symbol with no prints | Recorded as skipped; valid outcome. |
