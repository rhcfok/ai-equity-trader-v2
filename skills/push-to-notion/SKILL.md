---
name: push-to-notion
description: Publish dated Notion archive pages from Supabase trading data — a GEX Daily Briefing page (A→Z GEX map + strategy guidance, matching the established briefing format) under ai-trader-v2-kimi/gex, and the latest quant2_leaderboard run under ai-trader-v2-kimi/quant2 (matching the established leaderboard format). Use when pushing, archiving, or syncing GEX snapshots or the quant2 leaderboard to Notion, or when a daily pipeline step says "push to Notion".
---

# Push to Notion

Publishes two dated archive pages to Notion from Supabase. Page bodies follow
the formats already established in the `ai-trader-v2-kimi` Notion tree — do
not add, remove, or rename columns or sections.

| Surface | Source (latest only) | Notion parent | Page title | Icon |
|---|---|---|---|---|
| `gex` | `watchlist` categories + latest `gex_snapshot` row per symbol | `ai-trader-v2-kimi/gex` | `GEX Daily Briefing — <publish_date> <HH:MM> HKT` | 📊 |
| `quant2` | `quant2_runs` latest + `quant2_leaderboard` | `ai-trader-v2-kimi/quant2` | `quant2 Leaderboard — <publish_date>` | 📈 |

`<publish_date>` is the local date the push runs (YYYY-MM-DD), NOT the data
as-of date; the as-of date stays in the page body's summary line. `<HH:MM>` is
the current Hong Kong time when the payload is built. One child page per
surface per publish date — skip creation if it already exists.

## Established formats

- **gex page** (matches the existing `GEX Daily Briefing — <date> <HH:MM> HKT`
  pages): a `Source:` line (`latest gex_snapshot rows`, covered/total symbols,
  methodology note, "**Signed dealer positioning NOT computed**"), then a
  `## GEX Map & Strategy Guidance` table with exactly these columns —
  `Symbol | Category | Regime | Net GEX (unsigned) | Spot | Gravity | Call
  wall | Put wall | Validity | Above spot | Below spot | Guidance` — all
  watchlist symbols sorted A→Z. Net GEX shows `$+9.96M` (≥$1M) or `$+440.0K`
  (<$1M); Above/Below spot show the top-4 `gamma_ladder` strikes as
  `779(+4.685)`; Guidance is the map-only line or a 0–14 DTE credit-spread
  structure with buffer ≈ 0.5% of spot (snapped to $5 above $500).
- **quant2 page** (matches the existing `quant2 Leaderboard — <date>` pages):
  summary line (`N tickers scored as of <as_of> · holdout <start> → <end> ·
  validation **PASS** · validated candidates: **NONE** · probability status:
  \`not_validated\``), a factual `**Day notes:**` paragraph derived from the
  same rows (no editorial judgment), `## Top 25` with columns
  `rank | ticker | group | score | RSI(14) | active rules`, `## Group
  summary` with `group | tickers | avg score | avg RSI | oversold |
  trend-aligned`, a divider, and the italic research-only disclaimer.

## Procedure

1. Build the payload (JSON on stdout; progress goes to stderr):

```bash
python scripts/build_notion_payload.py gex      # or: quant2
# options: --date YYYY-MM-DD  --max-rows N (0=all; default 0 gex / 25 quant2)
#          --env-file PATH    (default: .env in cwd)
```

2. Resolve the Notion parent via the Notion plugin `search`: find the root
   page `ai-trader-v2-kimi`, then its child `gex` or `quant2`. Create a
   missing parent only if the user confirms.
3. Idempotency: search for an existing child page titled exactly
   `payload.title` under that parent. If it exists, skip creation and report
   "already published" — never create a duplicate or overwrite the page.
4. Create the page with `create-pages`: parent from step 2, `payload.title`,
   icon `payload.icon`, content `payload.markdown`.
5. If `payload.warnings` is non-empty (e.g. stale snapshots), surface it in
   the reply. Reply with page title + page ID + a one-line summary only —
   never paste the page body into the conversation.

## Credentials and boundaries

- The script only READS Supabase: env `SUPABASE_URL` /
  `SUPABASE_SERVICE_ROLE_KEY`, falling back to a `.env` file (keys
  `supabase_url` / `supabase_service_role`, either case). No credentials in
  this skill; no Notion token anywhere — Notion writes go through the Notion
  plugin's OAuth.
- The gex surface also reads the Supabase `watchlist` table
  (`symbol` + `category`; core / satellite / watch1) to attach watchlist
  categories.
- gex pages: all call/put measures are **unsigned** magnitudes (open interest
  is sign-neutral); never imply dealer long/short positioning.
- quant2 pages: the score is always labelled a historical frequency, never a
  calibrated probability. Push only runs whose `validation_overall` is `PASS`
  (the script shows it in the summary line; check before creating the page).

## Failure handling

| Symptom | Action |
|---|---|
| `missing SUPABASE_URL / ...` | Point `--env-file` at the project `.env` or export the env vars. |
| `no rows in gex_snapshot` / `quant2_runs` | The upstream pipeline has not run; do not invent data — report and stop. |
| `no watchlist rows in watchlist` | The watchlist table is empty/misnamed; report and stop. |
| Supabase HTTP 401/403 | Service-role key wrong or expired; do not retry in a loop. |
| quant2 `validation_overall` not `PASS` | Do not publish; report the validation state. |
| Notion parent `ai-trader-v2-kimi` (or its child) not found | Report and ask where to create it; do not create pages at the workspace root. |
| Notion `create-pages` fails on large table | Re-run the script with a smaller `--max-rows` and publish that payload. |
