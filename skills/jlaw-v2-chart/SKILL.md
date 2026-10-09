---
name: jlaw-v2-chart
description: Visual chart review of J Law trading candidates on the user's live TradingView "Kimi" layout. Use when asked to review/verify tickers on TradingView, eyeball charts for J Law setups, confirm screener survivors visually, or capture chart screenshots for the J Law workflow. This is the qualitative confirmation layer that complements the quantitative jlaw-v2-yahoo skill.
icon: candlestick-chart
color: Blue
---

# J Law Chart Review (TradingView visual layer)

This skill is the **eyeball layer** of the J Law workflow. `jlaw-v2-yahoo`
screens and gates a whole universe from Yahoo OHLCV data; this skill then
visually confirms the handful of survivors on the user's live TradingView
charts — base quality, MA geometry, 200MA slope, live pivot behaviour,
earnings flags — the things numbers do not capture well.

Keep the two skills separate: never run visual review on the whole universe;
only chart the checklist survivors (typically score ≥ 10, or the Watch/Valid
rows). A high runner score can never override a failing chart (trend/stage
items are absolute).

> Research and analysis only. No orders, no brokerage connection, no
> personalized financial advice.

## Boundaries

- Review only candidates handed over from `jlaw-v2-yahoo` (or explicitly
  named tickers); do not re-screen the universe visually.
- Chart findings **downgrade** but never upgrade a doctrine verdict:
  a Valid row with a broken chart becomes Watch/Skip; a Skip row never
  becomes Valid because the chart "looks nice".
- Never place orders or set positions — verdicts are research output.
- Do not set TradingView alerts unless the user explicitly asks.

## Required chart layout ("Kimi")

The user's customised TradingView layout (chart id `hnR03PEX`) must show:
daily candles + MA 10/20/50/200, Volume, MACD(12,26,9), RSI(14), ADX(14).
If any panel is missing, ask the user before reading — the checklist depends
on all of them.

## Bundled Resources

- `scripts/tv_capture.py`: captures per-ticker chart screenshots through
  Kimi WebBridge into a folder (stdlib only).
- `scripts/chart_upsert.py`: upserts the run's review JSON into Supabase
  `trade-jlaw-v2` with `jlaw_review_type="chart"` — every reviewed symbol,
  Skips included (stdlib only).
- `references/chart-review-checklist.md`: the ordered visual reading
  checklist and verdict inputs.

## Capture routes: desktop app (preferred) or browser

**Route A — TradingView desktop app (via kimi-cu-win).** The installed app
(`TradingView` in StartApps, appId
`TradingView.Desktop_n534cwy3pjxzj!TradingView.Desktop`) opens straight into
the user's cloud-synced "Kimi" layout with all required indicators. Use the
`kimi-cu-win` plugin: `list_windows()` → pick the window whose title matches
`<SYMBOL> ... / Kimi` → `get_window_state({include_screenshot: true})`.
Switch symbol by clicking the symbol box (top-left) and typing the ticker +
`Enter`; wait a few seconds for data, then observe again. Repeat per ticker.
Save/keep each screenshot for the review record.

**Route B — TradingView browser tab (via kimi-webbridge).** Use
`scripts/tv_capture.py` as below. Prefer this only when the desktop app is
unavailable or the user asks for the browser.

## Operating Workflow

0. **Pipeline context.** The full J Law workflow runs over the **Supabase
   `watchlist` universe — categories `core`, `satellite`, `watch1`** (see
   `jlaw-v2-yahoo` → "Default universe"): fetch symbols → screen with
   `jlaw-yahoo-runner` → gate with `jlaw-v2-yahoo` → chart-review survivors
   with this skill. This skill is the last stage; it never re-screens.
1. **Take the handoff.** Read the latest
   `jlaw_checklist_*.json` from `jlaw-v2-yahoo` (or use the tickers the user
   named). Select score ≥ 10 / Watch / Valid rows.
2. **Capture** (Route A desktop app, or Route B browser):

```bash
# Route B only:
python <skill_dir>/scripts/tv_capture.py \
  --from-checklist <checklist.json> --min-score 10 --out tv_review_<date>
# or explicit:  --symbols XOM:NYSE,V:NYSE,AAPL:NASDAQ --out tv_review_<date>
```

   Route B details: WebBridge daemon `http://127.0.0.1:10086/command`,
   session `jlaw-tv-chart`. Blank screenshots = backgrounded tab; the script
   already sends `Page.bringToFront` before each shot — re-shoot after a few
   seconds if one still comes out blank. Route A details: the desktop app
   window title confirms both symbol and layout (`DOCN ▼ 123.90 / Kimi`);
   verify the title matches the intended ticker before reading.
3. **Read each chart** against `references/chart-review-checklist.md`:
   context (day move, earnings flag) → trend/stage (price vs 200MA, 200MA
   slope, MA stack, extension) → base quality (VCP, tightness, volume
   dry-up, distance to pivot) → momentum (ADX / MACD / RSI) → risk geometry
   (chart-validated stop; RRR recompute if stop tightened).
4. **Merge with the doctrine row.** Produce per ticker: STRUCTURE, TRIGGER
   (buy-stop), STOP (chart-validated), ACTION, one-line reason. Downgrade
   where the chart fails; never upgrade.
5. **Report** in J Law style: regime line, verdict table (ticker, score,
   live price, MA structure, ADX, pivot/stop/target, action), the actionable
   setups first, then skips with reasons. Save the report to the workspace
   and list the screenshot folder.
6. **Record every reviewed symbol.** Write the run's review objects to
   `chart_review_<date>.json` (all of them — Valid, Watch, Skip, and
   `unreviewed`; nothing reviewed is dropped) and upsert to Supabase:

```bash
python <skill_dir>/scripts/chart_upsert.py --input chart_review_<date>.json
```

   Rows are written to `trade-jlaw-v2` with `jlaw_review_type = "chart"`
   under the `unique (run_date, symbol, jlaw_review_type)` constraint, so
   chart rows coexist with the quantitative `"yahoo"` rows for the same
   date/symbol — the two pipelines can run in parallel and be told apart by
   `jlaw_review_type`. Env: `SUPABASE_URL`, `SUPABASE_KEY`,
   `SUPABASE_V2_TABLE` (default `trade-jlaw-v2`). Only run the upsert after
   the operator approves result publication.

## Verdict Rules (chart layer)

- **Below a falling 200MA** → Skip (Stage-1 repair). No exceptions.
- **Earnings within ~2 weeks** → cap at Watch.
- **>5% above pivot** → chasing → Skip for new entry (holders trail).
- **ADX < 15** → coil only: entry valid strictly at pivot with tight stop;
  otherwise Watch.
- **Runner stop > 8%**: look for a tighter technical stop (MA shelf, base
  low); recompute RRR. If a chart-valid stop restores RRR ≥ 2 and stop ≤ 8%,
  the row may keep its Watch/Valid standing with the tightened stop noted.
- All-clear chart + all-gates-pass doctrine row → confirm as the actionable
  setup list with exact buy-stop / stop / target.

## Output Contract

Run file `chart_review_<date>.json`:
`{run_date, regime, reviews: [<review object>, ...]}`.

Per-ticker review object: `{symbol, live_price, day_change_pct, structure,
ma_notes, adx, macd_note, rsi, pivot, chart_stop, stop_pct, rrr_note,
earnings_flag, action, reason, failed_items?}` with
`action ∈ {Valid candidate, Watch, Skip, unreviewed}` and `failed_items`
listing the checklist items that failed (e.g. `["below_falling_200ma"]`).
**Every symbol reviewed in the run appears exactly once** — Valid, Watch,
Skip and unreviewed alike. The same JSON feeds `scripts/chart_upsert.py`,
which maps it into `trade-jlaw-v2` (`classification` ← action,
`current_price` ← live_price, `entry_price` ← pivot, `stop_loss` ←
chart_stop, `gates` ← the remaining notes) with `jlaw_review_type="chart"`.

## Failure Handling

- WebBridge unreachable / browser closed → say so and stop; do not fabricate
  chart readings. **Never invent a chart reading** — every claim must come
  from a screenshot actually captured and viewed this run.
- Screenshot unreadable (popup, wrong symbol, layout changed) → re-navigate
  and re-capture; if still unreadable, mark that ticker `unreviewed`.
- Layout missing a required indicator → pause and ask the user.

*This is research and analysis only, not personalized financial advice.*
