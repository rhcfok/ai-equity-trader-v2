---
name: substack-intelligence-daily
description: Daily intelligence digest from the MimiVsJames (主刊+即時策略), Dick Capital, and Tech-Stock Prospector newsletters — extracts every trading proposal from the last 2 weeks (entries, targets, stops, scenarios, dated) and builds a 14-day catalyst calendar, then pushes the summary to the Supabase `substack-intelligence` table and Notion. Use when asked for the daily substack summary, a trading-proposal recap from these letters, or the next-14-days catalyst calendar.
icon: newspaper
color: Red
---

# Substack Intelligence — Daily Skill

Produces a **daily summary** from three newsletters:
**MimiVsJames (主刊 + 即時策略)**, **Dick Capital**, **Tech-Stock Prospector**.

Two outputs, every run:
1. Every **trading proposal from the last 14 days** (ticker, direction, entry, target, stop, scenarios, conviction, post date) with live status marks.
2. A **catalyst calendar for the next 14 days** (dated events + why each matters in the letter's own framing).

## Environment (all on Ronald's machine)

- Pipeline + data live in `D:\AI projects\AI-trading\substack-intel\` (scripts in `scripts/`, saved posts in `posts/` as `<Author>__YYYY-MM-DD__<slug>.txt`, prices in `data/prices/`).
- HTTP egress only via local proxy `http://127.0.0.1:6268` (curl `-x`, or `scripts/lib.mjs` `proxiedFetch`). Yahoo quotes must go through **curl** (Node TLS is fingerprint-blocked).
- Paid access requires the Substack session: Kimi WebBridge daemon `http://127.0.0.1:10086/command` (JSON body via temp file, session `"substack-digest"`). Re-extract cookies → `tmp/substack_cookies.json` → **delete it when done**:
  1. `navigate` to `https://substack.com/`
  2. `cdp Storage.getCookies`, keep domains `.substack.com|damnang.com|fomosoc.com|photoncap.net|winginvesting.com`
  3. Custom-domain pubs need one `navigate` to `<domain>/account?lli=1` first (refreshes `connect.sid`).
- Supabase creds in `ai-equity-trader-v2/.env` (`SUPABASE_URL`, `SUPABASE_KEY` [service_role]). Table: **`substack-intelligence`** (schema below).
- Notion: MCP Notion tools; parent page **Substack Intelligence** id `3f7b8852-cf00-80c9-8b1d-d58e38acaf74` (under `ai-trader-v2-kimi`).

## Letter registry (daily set)

| key | name | base |
|---|---|---|
| mimivsjames | MimiVsJames 主刊 | https://mimivsjames.substack.com |
| mimivsjames2 | MimiVsJames 即時策略 | https://mimivsjames2.substack.com |
| dick | Dick Capital | https://dickcapital.substack.com |
| tsp | Tech-Stock Prospector | https://techstockprospector.substack.com |

## Procedure

1. **Scan for new posts** — for each pub `GET {base}/api/v1/archive?sort=new&limit=12` with cookie header; download anything newer than the newest saved file in `posts/` via `/api/v1/posts/{slug}` (plain text extraction; header block TITLE/URL/DATE/AUDIENCE/PAYWALL_DETECTED/BODY_CHARS).
2. **Refresh prices** (curl + proxy) for every ticker proposed in the window + backdrop (SPX ^GSPC, NDX ^NDX, SOXX, VIX ^VIX, US10Y ^TNX, NVDA, MU). Date-stamp: state whether marks are live-intraday or last close.
3. **Extract proposals** — grep the saved posts for the last 14 days: Mimi (`進場點位|停損|目標價|個股推薦|推薦`), Dick (`entry|position|added|scenarios|bear case`), TSP (`ACTION ALERT|Added|Removed|price target`). Include each letter's standing instructions (e.g., Mimi's reduce-into-rebounds calls, Dick's oil watch).
4. **Build the catalyst calendar** — dated events from the letters (CPI/PPI, ASML/TSMC prints, FOMC dates, mediations, IPO paths, earnings dates they name) + why-it-matters in their framing. Supabase carries prior calendars — carry forward anything unresolved.
5. **Write `daily-summary-YYYY-MM-DD.md`** — sections: Tape context → per-letter proposal tables (Date | Proposal | Levels | Status) → 14-day catalyst table → Bottom line.
6. **Push to Supabase** — POST one row to `substack-intelligence` (schema below; `summary_type='daily'`, edition `D-YYYY-MM-DD`). Remember: batch inserts need identical keys across objects.
7. **Push to Notion** — convert markdown tables to Notion `<table>` XML (pipe tables break on `|` in cells; see `substack-intel/scripts/md2notion.mjs`), create a child page under the Substack Intelligence parent titled `Daily Summary — YYYY-MM-DD (D-YYYY-MM-DD)`, then PATCH `notion_url` back into the Supabase row.
8. Purge `tmp/substack_cookies.json` and any CDP dumps.

## Supabase schema (`substack-intelligence`)

```
id BIGINT PK identity | created_at timestamptz | summary_type text ('daily'|'weekly')
edition text | summary_date date | window_start date | window_end date
letters text[] | title text | headline text
trading_proposals jsonb | catalyst_calendar jsonb | author_sections jsonb
market_backdrop jsonb | watchlist_flags jsonb | anticipation_status jsonb
raw_markdown text | notion_url text | source_files text[]
```

## Editorial rules

- **Never fabricate** — quote levels exactly as published; missing = "not stated"; paywalled = "paywalled".
- Status marks are the operator's own price checks; date-stamp them.
- Keep letter credibility ratings visible where known (Mimi 6/10, Dick 7/10, TSP 7/10 — see `notes/` in the pipeline folder).
