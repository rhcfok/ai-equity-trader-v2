---
name: substack-intelligence-weekly
description: Weekly digest from eight newsletters (FOMO, Damnang Research, Nutty, PhotonCap, MimiVsJames 主刊+策略, FUNDA, Dick Capital, Tech-Stock Prospector) in the fixed 9-section W1 format — coverage, market backdrop, classification, brief author-by-author, valuation freshness, 14-day catalysts, watchlist flags, forward-anticipation status, data note. Pushes to the Supabase `substack-intelligence` table and Notion. Use when asked for the weekly substack digest or the W-numbered weekly summary.
icon: calendar-days
color: Purple
---

# Substack Intelligence — Weekly Skill

Produces the **weekly digest (Wn)** from eight newsletters:
**FOMO · Damnang Research · Nutty · PhotonCap · MimiVsJames (主刊 + 即時策略) · FUNDA · Dick Capital · Tech-Stock Prospector.**

Format is fixed — mirror the W1/W2 structure exactly (Notion: *Weekly Digest — 2026-10-12 (W1)* and *(W2)*, page id `3f7b8852cf00-81c7-842d-e7f7be9a6b0b` under the **Substack Intelligence** parent `3f7b8852-cf00-80c9-8b1d-d58e38acaf74`).

## The 9 sections (W1 structure — keep the numbering)

1. **Coverage this week** — table: Letter | Posts (window) | New since last week | Notes. **Window = previous Sunday through this Saturday inclusive** (the run happens Sunday 10:00 local; cover the 7 completed days ending yesterday).
2. **Market backdrop** — table: Series | window-start | now | Week. Always include SPX, NDX, SOXX, NVDA, MU, 10Y (^TNX), VIX plus the week's rotation winners/losers. One prose paragraph: the tape's story in the letters' framing.
3. **Classification** — one line, unchanged from the audit (Group A = Damnang/PhotonCap/Nutty + Irrational/FUNDA/Gaetano/mark; Group B = BTYC/MimiVJ 主刊/Trader Joe/Money Machine/AsymBets; straddlers = MimiVJ-Strategy/FOMO/Wings/April/Dick/TSP).
4. **Author-by-author — brief** — 1–3 lines per letter covering **new posts only** (no re-summary of old material). Quote theses verbatim where load-bearing.
5. **Valuation freshness** — carry-forward table of live targets (MRVL 300–305/325–333, SONY 28.29/36.71, IMOS 100, FROG 130, LITE 1200, Dick's DECK/NICE scenarios, TSP's GLW 145…) + new ones; Price column + Status verdict (Live/Filled/Stale/Broken).
6. **Catalyst calendar — next 14 days** — dated events the letters flag.
7. **Watchlist flags** — Supabase `watchlist` tickers in the week's news, with one-line action framing (never move core; that's Ronald's call).
8. **Forward anticipations — status** — the 20-item dashboard from the 6-month audit §8; update statuses, keep numbering (3 = late-Oct pullback window, 7 = midterm rotation, 9 = MRVL entry, 14 = 2028 memory, 16 = ZS, 19 = AI-crowding…). Full list: `substack-intelligence-report.md` §8.
9. **Data note** — new posts saved, price timestamp, paywall caveats.

## Environment

Same as the daily skill (see `../daily/SKILL.md`): pipeline at `D:\AI projects\AI-trading\substack-intel\`, proxy `127.0.0.1:6268`, WebBridge `127.0.0.1:10086/command` session `substack-digest` for paid access (Mimi 主刊/策略, Dick, TSP, FOMO, Damnang, PhotonCap, Nutty are paid; FUNDA is free-tier previews only — quote previews as-is), curl-only Yahoo, Supabase creds in `ai-equity-trader-v2/.env`, cookies purged after use.

## Letter registry (weekly set)

mimivsjames / mimivsjames2 (substack), dick (dickcapital.substack.com), tsp (techstockprospector.substack.com), fomo (**www.fomosoc.com** custom), damnang (**www.damnang.com** custom), nutty (nuttycld.substack.com), photoncap (**photoncap.net** custom), funda (fundaai.substack.com).

## Procedure

1. Scan all eight archives for posts newer than saved; download new ones.
2. Refresh prices for the backdrop table + every live-target ticker; date-stamp (live vs close).
3. Deep-read ONLY the new posts (grep first: `目標價|進場|停損|target|entry|thesis|verdict|Preview|Review`); everything older is already digested in `notes/`, prior weeklies, and the audit.
4. Write `weekly-digest-YYYY-MM-DD.md` in the 9-section structure.
5. Push to Supabase `substack-intelligence` (`summary_type='weekly'`, edition `Wn-YYYY-MM-DD`, `summary_date`=run date, windows, letters[], structured `market_backdrop`/`watchlist_flags`/`anticipation_status`, full text in `raw_markdown`).
6. Convert tables → Notion `<table>` XML, create the child page under the Substack Intelligence parent, PATCH `notion_url` back to the row.
7. Purge cookies.

## Editorial rules

- Never fabricate; verbatim quotes grep-confirmed; "not stated"/"paywalled" where true.
- Section 4 stays **brief** (Ronald's rule — full author sections live in the 6-month audit).
- Credibility ratings for context: Damnang 8, FUNDA 8, FOMO 7, Dick 7, TSP 7, MimiVJ-Strategy 6, MimiVJ 主刊 6.5, Nutty N/A (research), PhotonCap N/A (maps).
