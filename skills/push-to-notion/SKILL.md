---
name: push-to-notion
description: Publish dated Notion archive pages from Supabase trading data — latest gex_snapshot rows to a child page under ai-trader-v2-kimi/gex, and the latest quant2_leaderboard run to a child page under ai-trader-v2-kimi/quant2. Use when pushing, archiving, or syncing GEX snapshots or the quant2 leaderboard to Notion, or when a daily pipeline step says "push to Notion".
---

# Push to Notion

Publishes two dated archive pages to Notion from Supabase:

| Surface | Source (latest only) | Notion parent | Page title | Icon |
|---|---|---|---|---|
| `gex` | `gex_snapshot` (latest row per symbol) | `ai-trader-v2-kimi/gex` | `GEX Snapshot — <publish_date>` | 📊 |
| `quant2` | `quant2_runs` latest + `quant2_leaderboard` | `ai-trader-v2-kimi/quant2` | `quant2 Leaderboard — <publish_date>` | 📈 |

`<publish_date>` is the local date the push runs (YYYY-MM-DD), NOT the data
as-of date; the as-of date stays in the page body's summary line. One child
page per surface per publish date — skip creation if it already exists.

## Procedure

1. Build the payload (JSON on stdout; progress goes to stderr):

```bash
python scripts/build_notion_payload.py gex      # or: quant2
# options: --date YYYY-MM-DD  --max-rows N (0=all; default 50 gex / 25 quant2)
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
| Supabase HTTP 401/403 | Service-role key wrong or expired; do not retry in a loop. |
| quant2 `validation_overall` not `PASS` | Do not publish; report the validation state. |
| Notion parent `ai-trader-v2-kimi` (or its child) not found | Report and ask where to create it; do not create pages at the workspace root. |
| Notion `create-pages` fails on large table | Re-run the script with a smaller `--max-rows` and publish that payload. |
