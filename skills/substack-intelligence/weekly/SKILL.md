---
name: substack-intelligence-weekly
description: "Weekly digest from nine newsletters plus sector-rotation monitoring. Produces a fixed 10-section Sunday–Saturday report, enriches it with IGV/IWM proxies, and applies eligible Supabase watchlist tier actions on real runs. Use for weekly Substack digests or W-numbered summaries."
---

# Substack Intelligence — Weekly Skill

Produce the weekly digest (Wn) from **FOMO · Damnang Research · Nutty · PhotonCap · MimiVsJames (主刊 + 即時策略) · FUNDA · Tech-Stock Prospector · StreetAlpha · Kova Investment Research**, with a concise market/sector-rotation read. **Dick Capital is excluded from the weekly coverage and author-by-author sections.**

## Required 10-section structure

Keep these section numbers and order. Write the complete digest to `raw_markdown` and mirror the current weekly style in Notion under the Substack Intelligence parent (`3f7b8852-cf00-80c9-8b1d-d58e38acaf74`). Put the compact data/coverage note at the end of Section 10; do not create an eleventh section.

1. **Market read** — concise judgment of leading, emerging, and weakening sectors. Separate price evidence from author interpretation and options-flow confirmation. Never turn the read into an order.
2. **Sector performance panel** — include all 11 SPDR Select Sector ETFs (**XLE, XLV, XLI, XLY, XLP, XLU, XLB, XLF, XLK, XLC, XLRE**), **IGV**, **IWM**, **QQQ**, and **SPY**. Show 1W, 1M, 3M, 6M, 1Y, and YTD returns on a common as-of date and return basis. Use simple price returns, not total returns, unless explicitly sourced otherwise. Label the SPDRs as sector ETFs; identify IGV as a software-industry proxy and IWM as a U.S. small-cap breadth proxy. IGV/IWM are not GICS sectors and must not enter sector-leader/laggard rankings. QQQ and SPY are context benchmarks. [IGV issuer page](https://www.ishares.com/us/products/239771/ishares-expanded-tech-software-sector-etf); [IWM issuer page](https://www.ishares.com/us/products/239710/ishares-russell-2000-etf).
3. **Market backdrop** — table `Series | window-start | now | Week`. Include SPX, NDX, SOXX, NVDA, MU, 10Y (^TNX), VIX, and rotation winners/losers. Add a concise paragraph on the market story as framed by the covered newsletters; do not duplicate the sector table.
4. **Coverage this week** — table `Letter | Posts (window) | New since last week | Notes`. Cover the nine-letter roster above. Exclude Dick Capital.
5. **Classification** — preserve the existing audit convention: Group A = Damnang/PhotonCap/Nutty + Irrational/FUNDA/Gaetano/mark; Group B = BTYC/MimiVJ 主刊/Trader Joe/Money Machine/AsymBets; straddlers = MimiVJ-Strategy/FOMO/Wings/April/Dick/TSP. This is a broader audit taxonomy, not the weekly coverage roster.
6. **Author-by-author — brief** — 1–3 concise lines per covered letter, **new posts only**. Include Damnang Research, PhotonCap, and Tech-Stock Prospector; exclude Dick Capital. Quote load-bearing theses verbatim and verify against the source. Do not treat a chart panel, company mention, or educational article as an explicit recommendation.
7. **Valuation freshness + underlying-stock review queue** — carry forward newsletter-sourced live targets/scenarios with price and `Live/Filled/Stale/Broken` status. Separately list (a) rotation-derived leaders/emerging-sector candidates supported by the UW screen and (b) explicit author-recommended candidates. Include sector, source lane, relevant returns, U.S. listing/common-stock verification, current Supabase tier, and recommended action. Do not blend system-derived candidates into the author’s proposal record.
8. **Catalyst calendar — next 14 days** — dated events the covered newsletters flag, the source letter, and why each matters.
9. **Watchlist flags** — report and apply eligible additions/promotions/downgrades on Supabase during a real run. Include both qualified sector-rotation candidates and explicit author recommendations; do not limit this section to tickers already mentioned in the newsletters. Exclude `hedge`/`hedges` rows entirely. Do not submit trade orders.
10. **Forward anticipations — status** — update the numbered 20-item dashboard from the 6-month audit §8 and retain its numbering. End with a compact data/coverage note: exact window, price as-of time, return basis, UW screener query/run time/completeness and audit path, missing/paywalled data, source coverage, and actions applied or not applied. Full list: `substack-intelligence-report.md` §8.

## Reporting window

- For a completed weekly edition, **window = previous Sunday through Saturday inclusive**; the regular run is Sunday 10:00 local and covers the seven completed calendar days ending the day before the run.
- Store those dates in `window_start` and `window_end`. Do not slide the window to the run date. Keep the price as-of timestamp separate from the reporting window.
- For weekly close-to-close returns, use the last available market close before the window opens as the baseline and the last available close on or before Saturday as the endpoint; state the exact dates. For 1M/3M/6M/1Y/YTD, use the same source's documented reference-close convention across every ETF/proxy/benchmark.
- A previously created early/partial row (including W2) is historical context; do not rewrite its window. Apply the fixed Sunday–Saturday rule to future editions.

## Market data and rotation rules

- Pull prices for the sector panel and backdrop from one consistent source and timestamp. Verify all 11 SPDR sector ETFs, IGV, IWM, QQQ, and SPY. If any series is unavailable, mark it missing rather than infer it from another ticker.
- Compute six price-return horizons deterministically from reported prices/reference closes. Preserve raw inputs, dates, and calculated returns in the structured payload. State when provider 1W is not an exact custom Sunday–Saturday close-to-close series.
- Rank sector winners/losers on the exact reporting window. Use 1M/3M/6M/1Y/YTD as context, not substitutes for the weekly rank. Rank IGV/IWM separately as thematic/breadth proxies, never as sectors.
- Treat Sector Tide/options activity as confirmation/caution, not ETF share flows. Identify its date/window; do not let one session override multi-horizon price leadership.
- Default sector weakness for watchlist review: the sector ETF underperforms SPY on **both 3M and 6M**. Record this test and its measured dates/returns. A different threshold may be used only when the user specifies it.
- Restrict stock candidates to `Common Stock` listings on explicitly verified U.S. exchanges. If UW omits venue, verify it from Yahoo Finance chart metadata; reject unresolved or non-U.S. listings. Never infer U.S. eligibility from ticker format or country alone.
- Use the configured UW Common Stock screen as the deterministic definition of **strong momentum** (see `skills/uw-stock-screener/references/screen-policy.md`): price above SMA50/SMA200, sufficient 11-month return, liquidity, and within the configured distance from SMA50/52-week high. Preserve the screen query, run time, and result.

## UW stock-screener handoff

The weekly workflow must consume the existing `skills/uw-stock-screener` workflow as its stock-discovery and momentum-validation stage; a citation to its policy alone is not sufficient. After identifying leading/emerging sectors and before finalizing the stock queue, run the existing script from the `ai-equity-trader-v2` repository root:

```bash
python3 skills/uw-stock-screener/scripts/uw_stock_screener.py
```

Use the default query in `skills/uw-stock-screener/references/screen-policy.md` and configured credentials. The runner writes a date-stamped audit JSON under `out/` and prints its output path; capture that path for the report/audit. **Do not pass `--apply`**: the runner must remain read-only so the weekly workflow can combine sector, stock-momentum, author, and current-watchlist evidence before one coordinated set of actions.

- Read the JSON audit's `candidates`, `rejected_or_unverified`, `result_set_truncated`, `run_at_utc`, `query`, `write_mode`, `watchlist_actions`, and write/error fields. Only `candidates` with verified U.S. exchange metadata are UW-qualified; honor `rejected_or_unverified`.
- Intersect UW candidates with leading/emerging sectors for the rotation-derived queue. Keep explicit author recommendations in a separate lane. Membership in the complete `candidates` set is the positive UW strong-momentum result. The screener does not establish sector leadership, author conviction, a J Law score, or a trade recommendation.
- Confirm `write_mode` is `dry_run`, `result_set_truncated` is false, and there are no run/write errors before using absence from `candidates` as evidence that a ticker failed the configured screen. If the file is missing, the run errors, or results are truncated/incomplete, report the screen as incomplete; do not infer a negative momentum result or downgrade a ticker whose Watch1 decision depends on that test.
- Treat the runner's `watchlist_actions` as preview/audit data only. Recompute final add/promote/downgrade actions under the weekly policy below; the standalone runner supports add/promote intake but does not implement sector-based downgrades.

## Weekly watchlist policy — real runs

This weekly workflow is authorized to apply eligible Supabase watchlist changes **without asking for separate user approval**. A real weekly run applies eligible actions after validating its sources; an explicitly requested `dry run`, `preview`, or `sample` must show proposed actions but make no watchlist changes.

- **Add → `watch1`:** add a new U.S.-listed common stock that is either a qualified stock in a leading/emerging sector and passes the configured UW screen, or an explicit positive author recommendation/position meeting listing and common-stock checks. Mere mentions, day-strength lists, shorts, educational examples, or stale targets alone do not qualify.
- **Promote → `watch1`:** promote an eligible existing lower-tier (`watch2`/`watch3`) ticker when it meets the same rotation or explicit-author criteria. Do not promote a symbol solely because its sector is weak or because it is exempt from a downgrade.
- **Downgrade `watch1` → `watch2`:** when its sector meets the weakness test, downgrade only if the ticker is not a Technology/semiconductor name **and** does not pass the complete strong-momentum screen. A strong-momentum ticker is eligible to remain at `watch1` even when its sector is weak. Record the stock-level screen result so the exception is auditable.
- If the UW screen is incomplete, do not use ticker absence to justify a downgrade; keep the affected Watch1 row unchanged and flag the decision as pending/incomplete.
- Keep `core`, `satellite`, `hedge`, and `hedges` categories unchanged in this weekly tier automation. Never delete watchlist records or alter unrelated fields.
- For every action, read the existing row by symbol before writing; act only when exactly one eligible row exists. If duplicates or mapping/listing ambiguity exists, make no change for that ticker and report it. For new symbols, re-read before insert to avoid duplicates. After writing, read back the row and record the before/after tier and status in Section 9 and `watchlist_flags`.
- Apply only `add`, `promote`, or `downgrade` actions. Do not create orders, alter brokerage/position fields, or trigger downstream trading pipelines.
- Never use the runner's `--apply` flag from this weekly workflow. Its read-only output is an input source; its add/promote-only behavior does not replace this workflow's combined, sector-aware action policy.

## Environment and weekly letter registry

Same as the daily skill (see `../daily/SKILL.md`): pipeline at `D:\AI projects\AI-trading\substack-intel\`, local proxy port resolved at runtime from Windows system proxy settings (it has moved between `127.0.0.1:6268` and `127.0.0.1:7892`), and WebBridge `127.0.0.1:10086/command` session `substack-digest` for paid access. Weekly coverage registry:

`fomo` (www.fomosoc.com), `damnang` (damnang.com), `nutty` (nuttycld.substack.com), `photoncap` (photoncap.net), `mimivsjames` + `mimivsjames2` (Substack main/strategy), `funda` (fundaai.substack.com), `tsp` (techstockprospector.substack.com), `streetalpha` (streetalpha.substack.com; post files `StreetAlpha__*`), and `kova` (kovainvest.substack.com; post files `KovaInvestmentResearch__*`). Dick Capital is not scanned or summarized by this weekly skill; the separate daily skill may continue to cover it.

Paid access follows the daily skill's cookie/session hygiene. FOMO, Damnang, PhotonCap, Nutty, and Mimi have paid content; FUNDA is free-tier previews only; StreetAlpha is bilingual with a light paywall; Kova is Ronald-free-tier with tiny previews and is a limited-visibility sentiment/breadth source. Mark missing or paywalled content; purge cookies and CDP dumps after use.

## Procedure

1. Scan the nine weekly archives for posts newer than saved; download new posts. Do not fetch or summarize Dick Capital in this weekly workflow.
2. Fix the completed Sunday–Saturday reporting window and verify its dates before collecting returns.
3. Refresh the market-backdrop series, the 11 SPDR ETFs, IGV/IWM, QQQ/SPY, and every live-target ticker. Date-stamp live versus close and retain reference prices.
4. Compute sector performance, weekly leaders/laggards, longer-horizon context, and separately ranked IGV/IWM proxies.
5. Deep-read **only new posts** (grep first: `目標價|進場|停損|target|entry|thesis|verdict|Preview|Review`). Cover Damnang, PhotonCap, and TSP when new posts exist. Older material is already in `notes/`, prior weeklies, and the audit.
6. Build the 14-day catalyst calendar; carry forward unresolved events from prior summaries.
7. Run the UW stock screener read-only as specified in **UW stock-screener handoff**; validate its JSON and completeness, then read the current watchlist excluding hedge/hedges rows. Build source-separated UW/rotation and author candidate queues; apply the weekly watchlist policy if this is a real run, and record each action result or failure accurately.
8. Write `weekly-digest-YYYY-MM-DD.md` in the 10-section structure. Keep author valuations/proposals separate from rotation-derived analysis and include the compact data note in Section 10.
9. Upsert one row in Supabase `substack-intelligence` using the existing schema, then create/update the Notion child page under the Substack Intelligence parent and patch `notion_url` to the same row.
10. Purge cookies and CDP dumps.

## Supabase storage contract

Use the existing `public."substack-intelligence"` schema; **no new columns or migrations** are required. On repeated runs, look up the edition first and update its existing row; insert only if no row exists. The table has no unique constraint on `edition`, so enforce idempotency in the application and verify the saved row after write.

| Column | Weekly content |
|---|---|
| `summary_type`, `edition`, `summary_date`, `window_start`, `window_end` | `weekly`, Wn identifier, run date, exact Sunday–Saturday dates |
| `letters`, `title`, `headline` | Nine-letter coverage list (exclude Dick), digest title, concise Section 1 read |
| `market_backdrop` (JSONB) | Preserve existing `asOf` and `week`; add `sectorRotation` with window dates, basis, 11 ETF rows, IGV/IWM proxy rows, QQQ/SPY benchmark rows, leaders/laggards, options confirmation, and stock review queue |
| `author_sections` (JSONB) | Section 6 keyed by covered letter; new posts only, no Dick Capital section |
| `trading_proposals` (JSONB) | Newsletter-sourced targets/scenarios only; never mislabel rotation candidates as author proposals |
| `catalyst_calendar` (JSONB) | Section 8 dated events |
| `watchlist_flags` (JSONB array) | Section 9 action candidates from author and rotation lanes, with reason, prior/new category, momentum/sector test, applied status, and any failure |
| `anticipation_status` (JSONB array) | Section 10, retaining the 20 audit item IDs |
| `raw_markdown` | Complete Sections 1–10; include the data note at the end of Section 10 |
| `source_files` (text[]) | Weekly Markdown, newly saved source-post files, and the UW screener JSON audit path |
| `notion_url` | Created/updated Notion child-page URL |

For the sector-rotation object, use explicit keys such as `windowStart`, `windowEnd`, `returnsBasis`, `etfs`, `sectorProxies`, `benchmarks`, `leaders`, `laggards`, `flowConfirmation`, and `stockReviewQueue`. Preserve the outer `market_backdrop` shape (`asOf`, `week`) for compatibility. Populate only fields supported by the schema/data; explain unavailable values in Markdown.

## Editorial rules

- Never fabricate. Verify quotes and dates. Use “not stated,” “paywalled,” or “unavailable” where appropriate.
- Keep Section 6 brief; full author detail belongs in the 6-month audit.
- Label every stock as newsletter-derived, sector-rotation-derived, or another explicit source lane.
- Distinguish recommendations, positions, chart mentions, and the model's own screen results. Watchlist actions are not orders.
- In a real run, apply the authorized watchlist rules without a confirmation prompt; in an explicit dry run, apply none and label all actions preview-only.
- Credibility ratings for weekly-covered sources: Damnang 8, FUNDA 8, FOMO 7, TSP 7, StreetAlpha 7 (caution/breadth calls 9 — weight risk warnings HIGH; momentum-phase PT timing 4 — weight fresh bull PTs after vertical runs LOW), MimiVJ-Strategy 6, MimiVJ 主刊 6.5, Kova 4 (limited visibility — use free posts as a real-time sentiment/risk datapoint only), Nutty N/A (research), PhotonCap N/A (maps).
- For StreetAlpha, track its SOX-breadth series and geopolitical-arc pieces in Section 6 when a new post appears. For Kova, cover only its free posts and clearly mark paywalled claims.
