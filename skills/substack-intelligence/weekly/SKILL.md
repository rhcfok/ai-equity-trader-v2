---
name: substack-intelligence-weekly
description: "Weekly digest from ten newsletters plus sector-rotation monitoring in a fixed 11-section Wn format. Use for weekly Substack digests or W-numbered summaries; writes to Supabase `substack-intelligence` and Notion."
---

# Substack Intelligence — Weekly Skill

Produce the weekly digest (Wn) from **FOMO · Damnang Research · Nutty · PhotonCap · MimiVsJames (主刊 + 即時策略) · FUNDA · Dick Capital · Tech-Stock Prospector · StreetAlpha · Kova Investment Research**, with a concise market/sector-rotation read.

## Required 11-section structure

Keep these section numbers and order. Write the complete digest to `raw_markdown` and mirror the current W1/W2 style in Notion under the Substack Intelligence parent (`3f7b8852-cf00-80c9-8b1d-d58e38acaf74`).

1. **Market read** — one concise judgment of leading, emerging, and weakening sectors. Separate price evidence from interpretation; mention material confirmation or divergence in options activity. Do not turn the read into a trade instruction.
2. **Sector performance panel** — table for all 11 SPDR Select Sector ETFs (**XLE, XLV, XLI, XLY, XLP, XLU, XLB, XLF, XLK, XLC, XLRE**) plus **QQQ** and **SPY** as context benchmarks. Show price returns for 1W, 1M, 3M, 6M, 1Y, and YTD; label as-of date and distinguish QQQ/SPY from sector ETFs. These are simple price returns, not total returns, unless explicitly sourced otherwise. Include the full panel in Markdown; use the concise leaders/laggards in Section 3.
3. **Market backdrop** — table `Series | window-start | now | Week`. Always include SPX, NDX, SOXX, NVDA, MU, 10Y (^TNX), VIX, and the rotation winners/losers. Keep a concise prose paragraph on the tape's story in the newsletters' framing. Do not duplicate the full sector table here.
4. **Coverage this week** — table `Letter | Posts (window) | New since last week | Notes`.
5. **Classification** — one line, unchanged from the audit: Group A = Damnang/PhotonCap/Nutty + Irrational/FUNDA/Gaetano/mark; Group B = BTYC/MimiVJ 主刊/Trader Joe/Money Machine/AsymBets; straddlers = MimiVJ-Strategy/FOMO/Wings/April/Dick/TSP.
6. **Author-by-author — brief** — 1–3 lines per letter, **new posts only**; do not re-summarize old material. Quote load-bearing theses verbatim and verify them against the source.
7. **Valuation freshness + underlying-stock review queue** — first carry forward newsletter-sourced live targets and scenarios (e.g. MRVL, SONY, IMOS, FROG, LITE, DECK/NICE, GLW) with price and `Live/Filled/Stale/Broken` status. Then present rotation-derived stock candidates in a clearly separate **review-only** subsection, with sector, rationale, relevant returns, current Supabase watchlist status, and suggested review—not as newsletter-author recommendations. An off-watchlist candidate is not a watchlist flag or an automatic add.
8. **Catalyst calendar — next 14 days** — dated events the newsletters flag, with the event source and why it matters.
9. **Watchlist flags** — Supabase `watchlist` tickers that also appear in the week's newsletter coverage, with one-line action framing. **Never move Core; enrollment, removal, or tier changes remain Ronald's decision.** Do not promote sector-screen candidates here unless they satisfy the newsletter-coverage and watchlist-match requirements.
10. **Forward anticipations — status** — update the numbered 20-item dashboard from the 6-month audit §8; retain its numbering. Full list: `substack-intelligence-report.md` §8.
11. **Data note** — posts saved, exact reporting window, price as-of/timing (live vs close), return basis, missing data, paywall caveats, and source limitations.

## Reporting window

- For a completed weekly edition, **window = previous Sunday through Saturday inclusive**; the regular run is Sunday 10:00 local and covers the seven completed calendar days ending the day before the run.
- Store those calendar dates in `window_start` and `window_end`. Do not slide the window to the run date. Keep the price as-of timestamp separate from the reporting window.
- For weekly close-to-close returns, use the last available market close before the window opens as the baseline and the last available close on or before Saturday as the endpoint; state the dates used. If generating a preliminary edition before Saturday, label it provisional and refresh the same edition after the window closes rather than redefining the window.
- A previously created early/partial row (including W2) is historical context; do not rewrite its window as part of this skill update. Apply the fixed Sunday–Saturday rule to future editions.

## Market-data and rotation rules

- Pull prices for the sector panel and backdrop from a consistent source and timestamp. Use the same endpoint/date convention for every ETF and benchmark; verify coverage for all 11 sector ETFs, QQQ, and SPY.
- Compute all six price-return horizons deterministically from the reported price and matching reference close. Preserve raw inputs/return values in the structured payload and cite the as-of timestamp. If any series is unavailable, mark it missing; never infer or fill it from another ticker.
- Rank weekly sector winners/losers only on the exact reporting window. Treat 1M/3M/6M/1Y/YTD as short-/medium-/longer-term context, not as substitutes for the weekly ranking.
- Treat Sector Tide/options activity as a confirmation/caution input, not as ETF share flows. Identify the date/window and do not let a single session override multi-horizon price leadership.
- Screen underlying stock candidates only after identifying leading/emerging sectors. Deduplicate against the Supabase watchlist; label each ticker as already tracked or not found. Record source/provenance and review status. Never change the watchlist automatically.

## Environment

Same as the daily skill (see `../daily/SKILL.md`): pipeline at `D:\AI projects\AI-trading\substack-intel\`, proxy `127.0.0.1:6268`, WebBridge `127.0.0.1:10086/command` session `substack-digest` for paid access (Mimi 主刊/策略, Dick, TSP, FOMO, Damnang, PhotonCap, Nutty are paid; FUNDA is free-tier previews only — quote previews as-is; StreetAlpha is bilingual EN/CN with a light paywall — most posts readable; Kova is Ronald-free-tier with tiny previews — use its free Trading Reports as real-time sentiment/breadth datapoints and mark the rest paywalled), curl-only Yahoo, Supabase creds in `ai-equity-trader-v2/.env`, cookies purged after use.

## Letter registry (weekly set)

mimivsjames / mimivsjames2 (Substack), dick (dickcapital.substack.com), tsp (techstockprospector.substack.com), fomo (**www.fomosoc.com** custom), damnang (**www.damnang.com** custom), nutty (nuttycld.substack.com), photoncap (**photoncap.net** custom), funda (fundaai.substack.com), streetalpha (streetalpha.substack.com — post files saved as `StreetAlpha__*`), kova (kovainvest.substack.com — post files saved as `KovaInvestmentResearch__*`).

## Procedure

1. Scan all ten archives for posts newer than saved; download new posts.
2. Fix the completed Sunday–Saturday reporting window and verify its dates before collecting weekly returns.
3. Refresh prices for the market-backdrop series, all sector ETFs + QQQ/SPY, and every live-target ticker. Date-stamp live vs close and retain reference prices.
4. Compute sector performance, weekly winners/losers, longer-horizon context, and the separate review-only stock queue. Check watchlist membership; do not modify it.
5. Deep-read **only new posts** (grep first: `目標價|進場|停損|target|entry|thesis|verdict|Preview|Review`); older material is already in `notes/`, prior weeklies, and the audit.
6. Build the 14-day catalyst calendar; carry forward unresolved events from prior summaries.
7. Write `weekly-digest-YYYY-MM-DD.md` in the 11-section structure. Keep newsletter-derived valuation/author claims separate from rotation-derived analysis.
8. Insert/upsert one row in Supabase `substack-intelligence` with `summary_type='weekly'`, edition `Wn-YYYY-MM-DD`, run date, correct `window_start`/`window_end`, `letters[]`, `title`, `headline`, structured JSONB below, full `raw_markdown`, and `source_files[]`.
9. Convert Markdown tables to Notion `<table>` XML, create the child page under the Substack Intelligence parent, then PATCH `notion_url` back to the same row.
10. Purge cookies and CDP dumps.

## Supabase storage contract

Use the existing `public."substack-intelligence"` schema; **no new columns are required** for the 11-section layout.

| Column | Weekly content |
|---|---|
| `summary_type`, `edition`, `summary_date`, `window_start`, `window_end` | `weekly`, Wn identifier, run date, exact Sunday–Saturday calendar dates |
| `letters`, `title`, `headline` | Covered newsletter names; digest title; concise Section 1 market read |
| `market_backdrop` (JSONB) | Preserve the existing `asOf` and `week` keys. Add `sectorRotation` with window dates, basis, ETF rows, benchmark rows, leaders/laggards, options confirmation, and review-only stock queue. |
| `author_sections` (JSONB) | Section 6, keyed by letter; new-post summaries only |
| `trading_proposals` (JSONB) | Section 7 newsletter-sourced live targets/scenarios and their status; do not mislabel rotation candidates as author proposals |
| `catalyst_calendar` (JSONB) | Section 8 dated events |
| `watchlist_flags` (JSONB array) | Section 9, only watchlist symbols also covered in the week's newsletters |
| `anticipation_status` (JSONB array) | Section 10, retaining audit item IDs |
| `raw_markdown` | Complete Sections 1–11 in order, including classification, valuation table, stock review queue, and Data note |
| `source_files` (text[]) | Weekly Markdown and newly saved source-post files |
| `notion_url` | Created Notion child-page URL |

For a sector-rotation object, use explicit keys such as `windowStart`, `windowEnd`, `returnsBasis`, `etfs`, `benchmarks`, `leaders`, `laggards`, `flowConfirmation`, and `stockReviewQueue`. Preserve the existing outer `market_backdrop` shape (`asOf`, `week`) for compatibility. Populate nullable structured fields when the corresponding section has data; otherwise use `null` only when the schema/consumer expects it, and keep the Markdown section explicit about unavailable data.

## Editorial rules

- Never fabricate. Verify verbatim quotes. Use “not stated,” “paywalled,” or “unavailable” where appropriate.
- Keep Section 6 brief; full author detail belongs in the 6-month audit.
- Label the source of every underlying-stock item as newsletter-derived or sector-rotation-derived.
- State that rotation candidates are research-review items, not orders or automatic watchlist changes.
- Credibility ratings for context: Damnang 8, FUNDA 8, FOMO 7, Dick 7, TSP 7, StreetAlpha 7 (caution/breadth calls 9 — weight its risk warnings HIGH; momentum-phase PT timing 4 — weight fresh bull PTs after vertical runs LOW), MimiVJ-Strategy 6, MimiVJ 主刊 6.5, Kova 4 (limited visibility — free posts as real-time sentiment/risk datapoint only; its 10/13 flip to 3x-short was the best-timed public action of any letter in October), Nutty N/A (research), PhotonCap N/A (maps).
- For StreetAlpha, explicitly track its SOX-breadth series and geopolitical-arc pieces (Iran/Hormuz war-to-deal model) in Section 6 — both were validated through the October crack. For Kova, Section 6 covers only its free posts (breadth reads, position postmortems like the 10/12 LITE stop-cascade honesty piece).
