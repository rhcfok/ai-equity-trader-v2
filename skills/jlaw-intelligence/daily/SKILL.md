---
name: jlaw-intelligence-daily
description: Daily screen of J Law's Master Your Trade member site (jlawstock.com) — captures and summarizes his latest 2 dated updates in 大盤板塊方向部署 (market risk-stance, sector deployment, key chart levels, week-over-week change) using the member login in the user's real browser via Kimi WebBridge. Use when asked for J Law's latest market view, the JLawStock daily/weekly update summary, his current Risk-On/Neutral/Risk-Off stance, or as the J Law section of the substack-intelligence daily digest.
icon: chart-candlestick
color: Gold
---

# J Law Intelligence — Daily Skill

Screens **J Law 大盤板塊方向部署** (Master Your Trade member site) and summarizes his **latest 2 dated video updates**. Lessons are posted ~weekly (Thu/Fri HK); a daily run detects anything new and keeps the summary current.

## Environment

- Member login lives in the user's real browser → all site access goes through **Kimi WebBridge** (daemon `http://127.0.0.1:10086/command`, session `jlaw-daily`). Read the `kimi-webbridge` skill first if unfamiliar.
- Capture script: `scripts/jlaw_capture.py` (bundled). Stdlib only.
- Category page (default): `https://www.jlawstock.com/products/master-your-trade/categories/2153346051` (大盤板塊方向部署). Adjacent sections exist on the product page — 股市分析 (weekly written report), 即時突發訊息 (breaking notes), 筆記 — expand scope only if asked.

## Procedure

1. **Capture** — run:
   ```
   python <skill_dir>/scripts/jlaw_capture.py --out <workspace>/runs/jlaw-YYYY-MM-DD --lessons 2 --interval 150
   ```
   Outputs per lesson: `<date>_page.txt` (page text incl. the stance block), `<date>_t*.jpg` (720p frames every 150s), plus `manifest.json`.
2. **Read the page text first** — the stance line `目前市場風險胃納：*Risk-On|Neutral|Risk-Off*` (asterisks = current setting) and any notes/Key Study List heading. This is his official stance; quote it exactly.
3. **Read the frames** — every frame, in time order. Charts carry the content: index/sector levels drawn, support/resistance, watchlist names on the right rail, macro slides (yields, FedWatch), and embedded Trader Circle posts.
4. **Write the summary** (markdown, `jlaw-summary-YYYY-MM-DD.md`):
   - **Stance** (verbatim label + what changed vs the prior update)
   - **Market view** — macro drivers he emphasizes (yields/TLT/FedWatch, index breadth/divergence)
   - **Sector & stock deployment** — what broke down / broke out / is being watched, with the exact levels he drew
   - **Key levels table** — ticker | level | context
   - **Delta vs previous update** — stance shifts, new/removed focus names
5. If combined with the substack daily: hand the summary to that flow (its step 5–7 handle file naming, Supabase `author_sections.jlaw`, and Notion).

## Hard-won technical facts (do not rediscover)

- **No transcripts exist.** Wistia embed JSON `captions` is always null; the 生成字幕 lessons are separate videos with burned-in subs, not text tracks. Frame capture is the only extraction path.
- **Never screenshot the player** — a paused/backgrounded Wistia player renders a frozen poster frame; screenshots at different timestamps look identical. The script instead injects an off-screen `<video crossorigin=anonymous>` pointed at the 720p asset from `https://fast.wistia.com/embed/medias/{hash}.json` (`hd_mp4_video`, width 1280) and draws seek positions to canvas. Wistia deliveries allow CORS, so the canvas is not tainted.
- **Background tabs throttle media to readyState 0** — the script calls CDP `Page.bringToFront` (focus emulation, does not steal the user's focus) after every navigate, and releases it at the end. If seeks still time out, re-run bringToFront.
- **Every lesson has its own Wistia hash** — read `wistia_async_<hash>` from the DOM of that lesson page; never reuse a hash across lessons (the 字幕 sibling is a different video of the same content).
- **Never guess post URLs** — a wrong post id redirects to the site home. Always take hrefs from the category page DOM.
- Windows shell mangles non-ASCII inline curl → the script talks to the daemon over `urllib`, no shell quoting involved.

## Editorial rules

- **Never fabricate** — quote stance labels and drawn levels exactly as shown; if a chart is illegible, say so. Frame sampling (default every 150s) can miss fast slide changes — note the sampling caveat once in the summary.
- J Law's content is **educational, not investment advice** (his own disclaimer); keep summaries descriptive of his view, never turn them into recommendations.
- Date-stamp everything: lesson date ≠ run date. A daily run with no new lesson is a valid result — report "no new update since <date>" and refresh only the delta section if his Trader Circle posts moved.
