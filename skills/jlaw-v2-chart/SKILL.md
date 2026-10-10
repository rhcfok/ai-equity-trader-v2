---
name: jlaw-v2-chart
description: Visual chart review of J Law watchlist tickers on the user's live TradingView "Kimi" layout. Use when asked to review/verify tickers on TradingView, eyeball charts for J Law setups, run the chart pipeline over the watchlist core/satellite/watch1 categories, or capture chart screenshots for the J Law workflow. This is the visual pipeline that runs in parallel with the quantitative jlaw-v2-yahoo skill.
icon: candlestick-chart
color: Blue
---

# J Law Chart Review (TradingView visual layer)

This skill is the **visual pipeline** of the J Law workflow. It runs in
parallel with `jlaw-v2-yahoo`: the yahoo pipeline screens the **entire**
Supabase `watchlist` table from OHLCV data; this skill reviews **every
ticker in `category` ∈ {core, satellite, watch1}** on the user's live
TradingView charts — base quality, MA geometry, 200MA slope, live pivot
behaviour, earnings flags — the things numbers do not capture well.

Both pipelines upsert to the same `trade-jlaw-v2` table, distinguished by
`jlaw_review_type` (`chart` here, `yahoo` there). Where both cover the same
symbol, a failing chart always overrides a flattering score — trend/stage
items are absolute.

> Research and analysis only. No orders, no brokerage connection, no
> personalized financial advice.

## Boundaries

- This is an **independent pipeline**, not a sub-step of `jlaw-v2-yahoo`:
  it reviews **every ticker in the Supabase `watchlist` table whose
  `category` is `core`, `satellite`, or `watch1`** (112 symbols as of
  2026-10-08), fetched fresh each run. It does not wait for, or depend on,
  the yahoo pipeline's output.
- Chart verdicts stand on their own as the visual review of record; when a
  yahoo row for the same symbol/date exists, chart findings may **downgrade**
  the combined view but never upgrade it.
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

0. **Pipeline context.** Two pipelines run in parallel over the Supabase
   `watchlist` table: `jlaw-v2-yahoo` screens the **entire** table from
   Yahoo data; **this skill** visually reviews the **`core` / `satellite` /
   `watch1` subset** on TradingView. Both upsert to `trade-jlaw-v2`, told
   apart by `jlaw_review_type` (`yahoo` vs `chart`).
1. **Fetch the universe.** Pull the review list straight from Supabase
   (project `vmxxmdtzvwizrpjrrvnp`):

```sql
select symbol, exchange from watchlist
where category in ('core','satellite','watch1');
```

   (`tv_capture.py --from-watchlist` does this automatically from
   `SUPABASE_URL` / `SUPABASE_KEY`.)
2. **Capture** every ticker in that universe (Route A desktop app, or Route
   B browser):

```bash
# Route B, full universe from Supabase:
python <skill_dir>/scripts/tv_capture.py --from-watchlist --out tv_review_<date>
# or a checklist/subset:  --from-checklist <checklist.json> --min-score 10
# or explicit:            --symbols XOM:NYSE,V:NYSE,AAPL:NASDAQ
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
4. **Verdict per ticker.** Produce: STRUCTURE, TRIGGER (buy-stop), STOP
   (chart-validated), TARGET, numeric RRR, CHART_SCORE (0–16 rubric in
   `references/chart-review-checklist.md` §5), ACTION, one-line reason.
   If a same-day `yahoo` row exists for the symbol, note
   agreement/conflict — the chart may downgrade the combined view, never
   upgrade it.
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

Per-ticker review object:
`{symbol, live_price, day_change_pct, structure, ma_notes, adx, macd_note,
rsi, pivot, chart_stop, stop_pct, target_price, rrr, vcp_stage,
chart_score, chart_score_breakdown, earnings_flag, action, reason,
failed_items?}`

- `action ∈ {Valid candidate, Watch, Skip, unreviewed}`; `failed_items`
  lists failed checklist items (e.g. `["below_falling_200ma"]`).
- `chart_score` is the **visual 0–16 A/B/C/D rubric** (see
  `references/chart-review-checklist.md` §5) — same scale and bands as the
  runner's score (0–9 Skip · 10–12 Watch · 13–14 Valid · 15–16 A+), so both
  pipelines are comparable in the table's `jlaw_score` column. It is
  **nullable by design** only when a chart was unreadable (`unreviewed`).
- `target_price` (measured move / next supply), `rrr` (numeric, from chart
  stop and target) and `vcp_stage` are numeric/text fields — record them
  whenever the chart supports them; leave null only when genuinely
  indeterminate (e.g. repair-stage charts with no base).
- `stop_pct` may be written in either sign convention in the review file;
  `chart_upsert.py` normalizes it to a **positive distance %**.

**Every symbol reviewed in the run appears exactly once** — Valid, Watch,
Skip and unreviewed alike. The same JSON feeds `scripts/chart_upsert.py`,
which maps it into `trade-jlaw-v2` (`classification` ← action,
`jlaw_score` ← chart_score, `current_price` ← live_price, `entry_price` ←
pivot, `stop_loss` ← chart_stop, `target_price`/`rrr`/`vcp_stage` direct,
`gates` ← the remaining notes + score breakdown) with
`jlaw_review_type="chart"`.

## Failure Handling

- WebBridge unreachable / browser closed → say so and stop; do not fabricate
  chart readings. **Never invent a chart reading** — every claim must come
  from a screenshot actually captured and viewed this run.
- Screenshot unreadable (popup, wrong symbol, layout changed) → re-navigate
  and re-capture; if still unreadable, mark that ticker `unreviewed`.
- Layout missing a required indicator → pause and ask the user.
- `chart_upsert.py` validates before writing: a `pivot` outside
  [0.5×, 2×] of `live_price` (misplaced percentages) or a `chart_stop`
  above 1.5× `live_price` is nulled, flagged in `failed_gates`, and
  reported as a `validation_warning` — never written silently.

*This is research and analysis only, not personalized financial advice.*
