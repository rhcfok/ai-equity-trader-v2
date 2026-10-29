---
name: push-to-notion
description: Publish dated Notion artefacts from local/Supabase trading data — a GEX Daily Briefing page (A→Z GEX map + strategy guidance, matching the established briefing format) under ai-trader-v2-kimi/gex, the latest quant2_leaderboard run under ai-trader-v2-kimi/quant2 (matching the established leaderboard format), and the daily 交易决策简报 (trading decision briefing) as one dated toggle on the J Law briefing page (3f5b8852cf008035916dd12bae2a3dd2). Use when pushing, archiving, or syncing GEX snapshots, the quant2 leaderboard, or the daily briefing to Notion, or when a daily pipeline step says "push to Notion".
---

# Push to Notion

Publishes two dated archive pages and one dated toggle to Notion. Bodies follow
the formats already established in the `ai-trader-v2-kimi` Notion tree — do
not add, remove, or rename columns or sections.

| Surface | Source (latest only) | Notion target | Title | Icon |
|---|---|---|---|---|
| `gex` | `watchlist` categories + latest `gex_snapshot` row per symbol | child page under `ai-trader-v2-kimi/gex` | `GEX Daily Briefing — <publish_date> <HH:MM> HKT` | 📊 |
| `quant2` | `quant2_runs` latest + `quant2_leaderboard` | child page under `ai-trader-v2-kimi/quant2` | `quant2 Leaderboard — <publish_date>` | 📈 |
| `briefing` | local `out/briefing_<date>.md` (written by the daily-trading-briefing skill) | **one dated `<toggle>` appended to the page `J Law 双管线 × Unusual Whales 期权流 — 交易决策简报`** (id `3f5b8852cf008035916dd12bae2a3dd2`) | the file's first H1, `📊 `-prefixed (e.g. `📊 交易决策简报 2026-10-29 — CW44 周四 \| …`) | 📊 |

`<publish_date>` is the local date the push runs (YYYY-MM-DD), NOT the data
as-of date; the as-of date stays in the page body's summary line. `<HH:MM>` is
the current Hong Kong time when the payload is built. One child page per
gex/quant2 surface per publish date — skip creation if it already exists. The
briefing surface creates/replaces exactly ONE toggle per date on the fixed
briefing page; never touch other dates' toggles.

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

- **briefing toggle** (matches the existing `📊 交易决策简报 <date> — …` toggles
  on the J Law briefing page): `<toggle>` first line = title, blank line, body,
  `</toggle>`. The builder converts the briefing markdown's pipe tables to
  `<table header-row="true"><tr><td>` syntax, escapes `\$` and `\<` outside
  tables, and drops the source H1 (it becomes the toggle title). Bilingual
  section structure comes from the source file — do not rewrite it.

## Procedure

1. Build the payload (JSON on stdout; progress goes to stderr):

```bash
python scripts/build_notion_payload.py gex      # or: quant2 | briefing
# options: --date YYYY-MM-DD  --max-rows N (0=all; default 0 gex / 25 quant2)
#          --env-file PATH    (default: .env in cwd)
# briefing only: --file PATH (default out/briefing_<date>.md under cwd)
#                --title "override toggle title"
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

### briefing surface procedure (toggle, not a page)

1. **Publish gate (mandatory, mirrors daily-trading-briefing Step 5.3):** run
   the check from `payload.check_command` —
   `python skills/daily-trading-briefing/scripts/flow_reconcile.py --date <date>
   --check-board <briefing.md> --strict` (run from the repo root, with the
   project `.env` credentials). It must exit 0. If it exits 1, do NOT publish;
   report the flagged label/staleness issues and stop.
2. Fetch the target page `3f5b8852cf008035916dd12bae2a3dd2` and look for a
   toggle whose first line starts with `📊 交易决策简报 <date>`.
3. If absent: `update-page` `insert_content` at position `end` with
   `payload.markdown` (one `<toggle>` block). If present: `update_content`
   replacing ONLY that toggle — copy `old_str` byte-exact from the fresh fetch
   (stored content double-escapes `\$` / `\|` / `\<`; prefer a plain-text tail
   anchor). Never modify other dates' toggles.
4. If `insert_content` is rejected, fall back to `replace_content` rebuilding
   the page with every existing toggle preserved verbatim plus the new one.

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
| `briefing file not found: out/briefing_<date>.md` | The daily-trading-briefing workflow has not produced today's file; do not publish — report and stop (never publish a stale date's file under today's title). |
| briefing `--check-board --strict` exits 1 | Label/staleness issues in the draft; do NOT publish. Report the issues verbatim and stop. |
