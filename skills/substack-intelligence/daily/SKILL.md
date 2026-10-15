---
name: substack-intelligence-daily
description: Daily intelligence digest from the MimiVsJames (主刊+即時策略), Dick Capital, and Tech-Stock Prospector newsletters plus J Law's latest 2 Master Your Trade video updates — extracts every trading proposal from the last 2 weeks (entries, targets, stops, scenarios, dated), J Law's current market stance and sector deployment, and builds a 14-day catalyst calendar, then pushes the summary to the Supabase `substack-intelligence` table and Notion. Use when asked for the daily substack summary, a trading-proposal recap from these letters, J Law's latest view inside the daily digest, or the next-14-days catalyst calendar.
icon: newspaper
color: Red
---

# Substack Intelligence — Daily Skill

Produces a **daily summary** from three newsletters:
**MimiVsJames (主刊 + 即時策略)**, **Dick Capital**, **Tech-Stock Prospector** —
plus **J Law 大盤板塊方向部署** (jlawstock.com member site; capture mechanics live in the bundled sibling skill `jlaw-intelligence-daily`, `skills/jlaw-intelligence/daily`).

Three outputs, every run:
1. Every **trading proposal from the last 14 days** (ticker, direction, entry, target, stop, scenarios, conviction, post date) with live status marks.
2. **J Law's current stance + sector deployment** from his latest 2 dated updates (stance label verbatim, key levels drawn, delta vs prior update).
3. A **catalyst calendar for the next 14 days** (dated events + why each matters in the letter's own framing).

## Environment (all on Ronald's machine)

- Pipeline + data live in `D:\AI projects\AI-trading\substack-intel\` (scripts in `scripts/`, saved posts in `posts/` as `<Author>__YYYY-MM-DD__<slug>.txt`, prices in `data/prices/`).
- HTTP egress only via local proxy `http://127.0.0.1:6268` (curl `-x`, or `scripts/lib.mjs` `proxiedFetch`). Yahoo quotes must go through **curl** (Node TLS is fingerprint-blocked).
- Paid access requires the Substack session: Kimi WebBridge daemon `http://127.0.0.1:10086/command` (JSON body via temp file, session `"substack-digest"`). Re-extract cookies → `tmp/substack_cookies.json` → **delete it when done**:
  1. `navigate` to `https://substack.com/`
  2. `cdp Storage.getCookies`, keep domains `.substack.com|damnang.com|fomosoc.com|photoncap.net|winginvesting.com`
  3. Custom-domain pubs need one `navigate` to `<domain>/account?lli=1` first (refreshes `connect.sid`).
- J Law access also rides WebBridge (member login in the user's browser), session `"jlaw-daily"` — no cookie extraction needed; the capture script handles the tab.
- Supabase creds in `ai-equity-trader-v2/.env` (`SUPABASE_URL`, `SUPABASE_KEY` [service_role]). Table: **`substack-intelligence`** (schema below).
- Notion: MCP Notion tools; parent page **Substack Intelligence** id `3f7b8852-cf00-80c9-8b1d-d58e38acaf74` (under `ai-trader-v2-kimi`).

## Letter registry (daily set)

| key | name | base |
|---|---|---|
| mimivsjames | MimiVsJames 主刊 | https://mimivsjames.substack.com |
| mimivsjames2 | MimiVsJames 即時策略 | https://mimivsjames2.substack.com |
| dick | Dick Capital | https://dickcapital.substack.com |
| tsp | Tech-Stock Prospector | https://techstockprospector.substack.com |
| jlaw | J Law 大盤板塊方向部署 | https://www.jlawstock.com/products/master-your-trade/categories/2153346051 |

`jlaw` is not a Substack letter — no archive API, no cookies. Source handling (WebBridge + Wistia frame capture) lives in `skills/jlaw-intelligence/daily`; this skill only consumes its summary.

## Procedure

1. **Scan for new posts** — for each pub `GET {base}/api/v1/archive?sort=new&limit=12` with cookie header; download anything newer than the newest saved file in `posts/` via `/api/v1/posts/{slug}` (plain text extraction; header block TITLE/URL/DATE/AUDIENCE/PAYWALL_DETECTED/BODY_CHARS).
2. **Screen J Law** — follow `skills/jlaw-intelligence/daily/SKILL.md`: run its `scripts/jlaw_capture.py --out <workspace>/runs/jlaw-YYYY-MM-DD --lessons 2`, read page text + frames, produce the stance/market-view/sector-deployment/key-levels/delta summary. If the browser or WebBridge daemon is unavailable, mark the section `unavailable today` and continue with the letters — do not block the run.
3. **Refresh prices** (curl + proxy) for every ticker proposed in the window + backdrop (SPX ^GSPC, NDX ^NDX, SOXX, VIX ^VIX, US10Y ^TNX, NVDA, MU). Date-stamp: state whether marks are live-intraday or last close.
4. **Extract proposals** — grep the saved posts for the last 14 days: Mimi (`進場點位|停損|目標價|個股推薦|推薦`), Dick (`entry|position|added|scenarios|bear case`), TSP (`ACTION ALERT|Added|Removed|price target`). Include each letter's standing instructions (e.g., Mimi's reduce-into-rebounds calls, Dick's oil watch).
5. **Build the catalyst calendar** — dated events from the letters (CPI/PPI, ASML/TSMC prints, FOMC dates, mediations, IPO paths, earnings dates they name) + why-it-matters in their framing. Merge any dated events J Law flagged (he tracks FOMC/FedWatch closely). Supabase carries prior calendars — carry forward anything unresolved.
6. **Write `daily-summary-YYYY-MM-DD.md`** — sections: Tape context → J Law stance & sector deployment → per-letter proposal tables (Date | Proposal | Levels | Status) → 14-day catalyst table → Bottom line.
7. **Push to Supabase** — POST one row to `substack-intelligence` (schema below; `summary_type='daily'`, edition `D-YYYY-MM-DD`, `letters` includes `'jlaw'`, his summary goes in `author_sections.jlaw`). Remember: batch inserts need identical keys across objects.
8. **Push to Notion** — convert markdown tables to Notion `<table>` XML (pipe tables break on `|` in cells; see `substack-intel/scripts/md2notion.mjs`), create a child page under the Substack Intelligence parent titled `Daily Summary — YYYY-MM-DD (D-YYYY-MM-DD)`, then PATCH `notion_url` back into the Supabase row.
9. Purge `tmp/substack_cookies.json` and any CDP dumps.

## Supabase schema (`substack-intelligence`)

```
id BIGINT PK identity | created_at timestamptz | summary_type text ('daily'|'weekly')
edition text | summary_date date | window_start date | window_end date
letters text[] | title text | headline text
trading_proposals jsonb | catalyst_calendar jsonb | author_sections jsonb
market_backdrop jsonb | watchlist_flags jsonb | anticipation_status jsonb
raw_markdown text | notion_url text | source_files text[]
```

## TSP callout analytics overlay (event-study based — include in every daily)

A 2026-10-13 event study (576 dated, price-stamped TSP callouts; `substack-intel/data/tsp_event_study.json`; scripts `scripts/tsp_events.mjs` + `scripts/tsp_stats2.mjs`) established that TSP callouts carry **two opposite short-term signals**. Every daily summary must apply this to TSP content:

| TSP event type | Historical short-term behavior | How to annotate in the daily |
|---|---|---|
| **ACTION ALERT** (explicit dated add/remove with price/PT) | Cleanest signal: GLW +35.4% (PT exceeded), BRZE +14.3%/1wk, WDAY sold +113.6% | Flag as **high-weight** — quote entry/PT verbatim |
| **First-time portfolio tag** (Vulture/Small-Cap) | **Post-tag drift DOWN: median −10.5%, hit 17% at 20d** (12 vulture, 8 smallcap events); recovers long-run (vulture 92% up, μ +49.8%) | Flag: *"expect post-tag drift — historically better entry 2–4 weeks after the tag"* |
| **News-recap mention** (non-portfolio) | Momentum tell: +6.4% median 5d, 75% hit; +13.8% μ 20d | Treat as a short-term momentum screen item |
| Split artifacts | Stated prices can be pre-split (CRWD 4:1 found) | Cross-check stated price vs file close; ratio >1.5 or <0.67 → use adjusted close |

Standing stats block (reproduce in the daily's TSP section; refresh via the two scripts when new first-tag events accrue):

```
First Vulture tag (n=12):  20d μ −13.2% / hit 17%  → now μ +49.8% / hit 92%
First Small-Cap tag (n=8): 20d μ −10.8% / hit 38%  → now μ +34.0% / hit 50%
News mentions (n=36):      5d μ +6.4% / hit 75%    → now μ +60.5% / hit 78%
All mentions (n=576):      20d μ +5.9% / hit 60%   → now μ +41.6% / hit 86%
Benchmark: SPX +7.6–11.1% same spans; first-callout median excess +16.7% (64% hit)
Caveats: single regime (2026 software/memory bull), small n per tag group, clustered weekly re-mentions
```

## Editorial rules

- **Never fabricate** — quote levels exactly as published; missing = "not stated"; paywalled = "paywalled". Same for J Law: quote his stance label and drawn levels exactly; his content is educational — describe his view, never convert it into a recommendation.
- Status marks are the operator's own price checks; date-stamp them.
- Keep letter credibility ratings visible where known (Mimi 6/10, Dick 7/10, TSP 7/10 — see `notes/` in the pipeline folder).
