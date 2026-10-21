---
name: substack-intelligence-daily
description: Daily intelligence digest from the MimiVsJames (主刊+即時策略), Dick Capital, Tech-Stock Prospector, Kova Investment Research, and Street Alpha newsletters plus J Law's 大盤板塊方向部署 stance — extracts every trading proposal from the last 2 weeks (entries, targets, stops, scenarios, dated), a computed SOX-breadth debrief (Street Alpha's indicator), J Law's current stance, and a 14-day catalyst calendar, then pushes the summary to the Supabase `substack-intelligence` table and Notion. Use when asked for the daily substack summary, a trading-proposal recap, J Law's stance, the SOX-breadth reading, or the next-14-days catalyst calendar.
icon: newspaper
color: Red
---

# Substack Intelligence — Daily Skill

Produces a **daily summary** from six sources:
**MimiVsJames (主刊 + 即時策略)**, **Dick Capital**, **Tech-Stock Prospector**,
**Kova Investment Research**, **Street Alpha**, **J Law 大盤板塊方向部署**.

Three outputs, every run:
1. Every **trading proposal from the last 14 days** (ticker, direction, entry, target, stop, scenarios, conviction, post date) with live status marks.
2. A **SOX-breadth daily debrief** (Street Alpha's indicator, computed locally, cross-checked against his published readings).
3. J Law's **stance** + a **catalyst calendar for the next 14 days**.

## Access architecture (post-Kimi — rewritten 2026-10-18)

**Kimi WebBridge is GONE (subscription ended). The PRIMARY path is the ZCode in-app browser via the `browser-use:control-browser` skill (Node REPL MCP `mcp__node_repl__js`).** Every step below is proven working end-to-end.

1. **In-app browser setup** — bootstrap `browser-client.mjs` from `process.env.ZCODE_PLUGIN_ROOT`; `agent.browsers.get("iab")`; one tab reused for everything. Tabs persist for the ZCode process lifetime; if `tabs.list()` is empty, create one.
2. **Substack login** (once per process lifetime; verify by navigating to `substack.com` and checking the sidebar shows 订阅/Subscriptions): `substack.com/sign-in` → fill email → the email-code screen appears → click 使用密码登录 via a **page-context `.click()`** (native Playwright clicks time out on these React forms) → fill password → submit. Credentials in `G:\其他计算机\Honor laptop\ai_credentials\.env` (`substack_login` / `substack_password`; never echo them). The session carries Ronald's paid subscriptions — **paid-tier posts come back complete**.
3. **Fetching** (no cookies, no CORS) — navigate the tab directly to `{base}/api/v1/archive?sort=new&limit=12` and `{base}/api/v1/posts/{slug}`, then `JSON.parse(await tab.playwright.evaluate(() => document.body.innerText))`. Cross-origin `fetch()` is CORS-blocked; direct navigation carries the session. Custom-domain pubs (damnang.com, fomosoc.com, photoncap.net, winginvesting.com) authenticate through this session via their `/account?lli=1` handshake.
4. **Prices** — navigate to `query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d` (^GSPC/^VIX/^NDX/^TNX for SPX/VIX/NDX/US10Y), parse innerText, save `data/prices/{sym}.json` as `{symbol, meta:{price,time}, timestamps, quotes:{close,high,low}}`. Yahoo's raw key is `timestamp` (singular) — normalize to `timestamps` on save.
5. **Supabase writes** — navigate to the PostgREST root (`{SUPABASE_URL}/rest/v1/`), then same-origin `fetch()` POST/PATCH from `tab.playwright.evaluate` with `apikey` + `Authorization: Bearer` (service-role key from `ai-equity-trader-v2/.env`, never echoed).
6. **J Law** (no WebBridge needed) — log the same tab into `jlawstock.com/login` with `jlawstock_email`/`jlawstock_password` (submit via `input[type=submit]` click in evaluate). Navigate `https://www.jlawstock.com/products/master-your-trade/categories/2153346051`; extract the stance line `風險胃納：...` and the lesson list from page text. Video-frame content requires a vision-capable session — without it, publish stance + lesson titles and mark `video content not extracted`.
7. **TradingView** (layout checks, e.g. `street_alpha_sox`) — use the `computer-use:computer-use` skill (native app, a11y-first; window title carries symbol/price/layout, `getAXState` reads legends/news).
8. **Optional accelerators** — a local proxy may exist (resolve the current port from Windows system proxy settings at runtime; it has moved between `127.0.0.1:6268` and `127.0.0.1:7892`) enabling shell `curl -x` / `scripts/lib.mjs proxiedFetch`; use only if present, never as a dependency.

## Letter registry (daily set)

| key | name | base |
|---|---|---|
| mimivsjames | MimiVsJames 主刊 | https://mimivsjames.substack.com |
| mimivsjames2 | MimiVsJames 即時策略 | https://mimivsjames2.substack.com |
| dick | Dick Capital | https://dickcapital.substack.com |
| tsp | Tech-Stock Prospector | https://techstockprospector.substack.com |
| kova | Kova Investment Research | https://kovainvest.substack.com |
| streetalpha | Street Alpha | https://streetalpha.substack.com |
| jlaw | J Law 大盤板塊方向部署 | https://www.jlawstock.com/products/master-your-trade/categories/2153346051 |

`kova` = Chinese momentum-trader ("做深度研究的动量交易者"); his account-return claims (+52% April / +64% May) are marketing — never reproduce as performance; unrated until the first event study.
`streetalpha` (resolved 2026-10-17): former buyside analyst, bilingual EN/中文, global macro & equities, 1,000+ subs — Ronald subscribes.
`jlaw` is not a Substack letter — no archive API; access = IAB login + page text (above). Video-frame capture mechanics from the old `jlaw-intelligence-daily` sibling skill apply only when a vision-capable session is available.

## Procedure

1. **Scan for new posts** — all six Substack pubs via step 3; download anything newer than the newest saved file in `posts/` (header block TITLE/URL/DATE/AUDIENCE/PAYWALL_DETECTED/BODY_CHARS; save both the `.txt` and raw JSON under `data/raw/<key>/`).
2. **Screen J Law** — IAB login + category page text (stance + lesson list). If unavailable, mark `unavailable today` and continue — do not block the run.
3. **Refresh prices** — every ticker proposed in the window + backdrop (SPX, NDX, SOXX, VIX, US10Y, NVDA, MU). Date-stamp whether marks are live-intraday or last close.
4. **Extract proposals** — grep saved posts for the last 14 days: Mimi (`進場點位|停損|目標價|個股推薦|推薦`), Dick (`entry|position|added|scenarios|bear case`), TSP (`ACTION ALERT|Added|Removed|price target`), Kova (`買點|進場|停損|目標|動能|佈局|減碼`), Street Alpha (`entry|target|stop|buy|add|trim|position`). Include standing instructions (e.g., Mimi's reduce-into-rebounds calls, Dick's oil watch).
4b. **SOX-breadth daily debrief (Street Alpha section)** — run `node scripts/sox_breadth.mjs` in `substack-intel/`; if node is not on PATH (true since 2026-10-21), run the identical Python port instead: `ai-equity-trader-v2/.venv/Scripts/python.exe scripts/sox_breadth.py` (same universe, same output). Both reproduce his indicator from `data/prices/*.json` (30 SOX names from his Pine script, QRVO→ARM swap applied; validated 1:1 against his published 10/16 reading). Ensure all 30 files are fresh first (step 3). Emit the fixed block: `As of {date} close: cyan X · orange Y · magenta Z · gray10 G · orange-percentile P% · stripe · buy-trigger state · context (magenta≥40 bull-pullback / <20 bear-break) · trend_6d`. Semantics per his manual: oversold guarantees nothing — the green triangle (orange crossing ABOVE gray with percentile <10) is the only buy trigger; magenta ≥40 = pullback within a bull market; tops signals = tighten stops, never short alone. If the script cannot run (no node), carry the last reading marked `carried — not recomputed`.
5. **Build the catalyst calendar** — dated events from the letters (CPI/PPI, ASML/TSMC prints, FOMC, mediations, earnings dates, IPO paths, elections) + why-it-matters in their framing. Carry forward anything unresolved from the newest daily row in Supabase.
6. **Write `daily-summary-YYYY-MM-DD.md`** — Tape context → per-letter proposal tables (Date | Proposal | Levels | Status) → SOX-breadth debrief block → 14-day catalyst table → Bottom line.
7. **Push to Supabase** — POST one row to `substack-intelligence` (`summary_type='daily'`, edition `D-YYYY-MM-DD`, `letters` includes all six keys + `'jlaw'`, `author_sections.sox_breadth` + `author_sections.jlaw`, `raw_markdown` = full text). Batch inserts need identical keys across objects.
8. **Push to Notion** — convert markdown tables to Notion `<table>` XML (`substack-intel/scripts/md2notion.mjs`), create a child page under parent `3f7b8852-cf00-80c9-8b1d-d58e38acaf74` titled `Daily Summary — YYYY-MM-DD (D-YYYY-MM-DD)`, PATCH `notion_url` back into the row.
9. No cookie files to purge (the IAB holds the session itself).

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

A 2026-10-13 event study (576 dated, price-stamped TSP callouts; `substack-intel/data/tsp_event_study.json`; scripts `scripts/tsp_events.mjs` + `scripts/tsp_stats2.mjs`) established two opposite short-term signals. Apply to every TSP item:

| TSP event type | Historical behavior | Annotation |
|---|---|---|
| **ACTION ALERT** (dated add/remove with price/PT) | Cleanest signal: GLW +35.4% (PT exceeded), BRZE +14.3%/1wk, WDAY +113.6%, VRNS +121% realized | Flag **high-weight** — quote entry/PT verbatim |
| **First-time portfolio tag** (Vulture/Small-Cap) | Post-tag drift DOWN: median −10.5%, 17% hit at 20d; recovers long-run | Flag *"expect post-tag drift — better entry 2–4 weeks after the tag"* |
| **News-recap mention** | Momentum tell: +6.4% median 5d, 75% hit | Treat as short-term momentum screen |
| Split artifacts | Stated prices can be pre-split (CRWD 4:1) | Cross-check ratio >1.5 or <0.67 → use adjusted close |

## Editorial rules

- **Never fabricate** — quote levels exactly as published; missing = "not stated"; paywalled = "paywalled". Same for J Law: quote his stance label exactly; describe, never convert to a recommendation.
- Status marks are the operator's own price checks; date-stamp them.
- Credibility ratings: Mimi 主刊 6.5/10, Mimi 策略 6/10, Dick 7/10, TSP 7/10 — Kova and Street Alpha start **unrated** until a 14-day window of checkable calls accumulates (see `notes/`).
