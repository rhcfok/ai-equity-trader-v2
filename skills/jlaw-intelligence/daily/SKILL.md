---
name: jlaw-intelligence-daily
description: Daily screen of J Law's Master Your Trade member site (jlawstock.com) — captures his latest 大盤板塊方向部署 updates (market risk-stance, sector deployment, key levels) using the ZCode in-app browser with the member login, replacing the retired Kimi WebBridge capture. Use when asked for J Law's latest market view, his current Risk-On/Neutral/Risk-Off stance, or as the J Law section of the substack-intelligence daily digest.
icon: chart-candlestick
color: Gold
---

# J Law Intelligence — Daily Skill

Screens **J Law 大盤板塊方向部署** (Master Your Trade member site) and summarizes his **latest 2 dated video updates**. Lessons are posted ~weekly (Thu/Fri HK); a daily run detects anything new and keeps the summary current.

## Access architecture (post-Kimi — rewritten 2026-10-18)

Kimi WebBridge is retired (subscription ended). Primary access = **ZCode in-app browser** via the `browser-use:control-browser` skill (Node REPL MCP `mcp__node_repl__js`), proven working 2026-10-18:

1. Bootstrap `browser-client.mjs` from `process.env.ZCODE_PLUGIN_ROOT`; `agent.browsers.get("iab")`; reuse the existing tab (same tab used for Substack — both logins coexist).
2. **Login** (once per process lifetime): navigate `https://www.jlawstock.com/login`; fill the `Email`/`Password` textboxes; submit via page-context `document.querySelector('input[type=submit]').click()` in `evaluate` (native Playwright clicks time out on this form). Credentials in `G:\其他计算机\Honor laptop\ai_credentials\.env` (`jlawstock_email` / `jlawstock_password`, never echo). Success = URL becomes `.../library`.
3. **Read the category page**: `https://www.jlawstock.com/products/master-your-trade/categories/2153346051` — extract the stance line matching `風險胃納[：:]\s*.{0,30}` and the dated lesson list (`N Oct 2026` entries, newest first).
4. **Lesson depth**: the stance lives in each lesson's page text when present; full chart/level content is in the Wistia video frames, readable only by a vision-capable session (the old `scripts/jlaw_capture.py` frame pipeline was built on WebBridge commands and is now retired). Without vision: publish stance + lesson titles + any page text, marked `video content not extracted`.

## Procedure

1. Login + category page (above).
2. Read the **latest lesson's page text** for the stance line and any notes; list the 2 newest lesson titles/dates.
3. Write the summary:
   - **Stance** — verbatim label (`目前市場風險胃納：*Neutral*` style; asterisks = current setting), never rephrased.
   - **Delta vs prior update** — stance shift or new focus names if the previous run's summary exists in `runs/` or the daily's prior row.
   - **Lesson list** — the 2 newest dated lessons with titles.
4. Hand the summary to the substack daily flow (its step 7–8 handle `author_sections.jlaw`, Supabase, and Notion).
5. If login fails or the site is unreachable, publish `unavailable today` and do not block the daily.

## Known-good facts (do not rediscover)

- The stance line IS in the category/list page text when the lesson exposes it (verified 2026-10-18: `風險胃納：*Neutral*`); some lessons (e.g. 16 Oct) do not expose it in page text — then carry the last known stance and mark it.
- Login persists in the IAB profile across the ZCode process lifetime; `tabs.list()` empty means the process restarted — re-login (Substack too).
- No transcripts exist for the videos; frame capture is the only deep-content path and needs vision.
