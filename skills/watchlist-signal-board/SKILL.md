---
name: watchlist-signal-board
description: Generate a daily consensus board comparing a fresh technical scan (SMA/RSI/MACD/EMA/ADX composite) against all upstream signals — jlaw-v2-yahoo, jlaw-v2-chart, option-flow verdicts, quant2 leaderboard, and stldc trend — across the whole Supabase watchlist, flagging high-conviction names and bull/bear conflicts. Use when asked to scan the watchlist, compare signals, build the signal board, find where fresh scans agree or disagree with J Law / flow / quant2 / stldc, or rank the watchlist by consensus.
icon: layout-dashboard
color: Purple
---

# Watchlist Signal Board

Runs a **fresh technical scan** over every watchlist symbol and lays it
side-by-side with the five upstream signal runs in Supabase — `trade-jlaw-v2`
(yahoo **and** chart review types), `flow_verdict_rows`, `quant2_leaderboard`,
and `trade-stldc-v2` — then votes each symbol BULL / BEAR / NEUT per signal and
ranks the whole watchlist by consensus strength.

> Research and analysis only. No orders, no brokerage connection, no
> personalized financial advice.

## Why this exists

Each upstream pipeline answers a different question, on its own schedule:

| Signal | Table | Answers |
|---|---|---|
| Fresh scan | (computed live) | Is there a bullish trend *right now*? (composite /~9.5) |
| jlaw-v2-yahoo | `trade-jlaw-v2` (`jlaw_review_type=yahoo`) | Does the symbol pass J Law's quantitative buy checklist? |
| jlaw-v2-chart | `trade-jlaw-v2` (`jlaw_review_type=chart`) | Does the *chart* show the setup? (base quality, MA geometry) |
| flow verdict | `flow_verdict_rows` | Where is option flow leaning? (bullish/bearish/mixed + premium tilt) |
| quant2 | `quant2_leaderboard` | Which beaten-down names are *rebuilding*? (weakness-repair LONG model) |
| stldc | `trade-stldc-v2` | Trend direction + duration context (and SPY regime) |

Any single signal lies. The board is for the **agreement structure**: unanimous
names, near-unanimous names, and — equally useful — the conflicts (e.g. scan
bullish but flow bearish) where the next look should go.

## Universe

Every symbol in `public.watchlist` with `category` ∈ {core, satellite, watch1,
watch2, watch3}, filtered to US option-listed exchanges (NASDAQ / NYSE / AMEX /
ARCA / BATS / CBOE). Rows with a NULL `exchange` are kept when the symbol is a
plain US ticker; explicitly non-US exchanges (OTC, LSE, Stockholm, XETR, …)
are dropped. `--categories` narrows; `euro` is excluded by default.

## Credentials

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` environment variables, or
`--creds <path>` pointing at a file containing `supabase_url=` and
`supabase_service_role=` lines. The key is never logged.

## Instructions

```bash
python3 skills/watchlist-signal-board/scripts/watchlist_signal_board.py \
  --output-dir reports
```

- `--period` — scan lookback (1mo/3mo/6mo, default 3mo)
- `--workers` — parallel scan threads (default 10; ~300 symbols ≈ 60–90s)
- `--limit N` — cap universe size (debug)
- Dependencies: `yfinance`, `pandas`, `requests` (self-contained script —
  indicators are computed in pandas, no pandas-ta dependency)

Outputs (timestamped, like every other pipeline here):
`reports/watchlist_signal_board_<date>_<HHMM>.{json,md}`

### Supabase destination

By default the board is **upserted** to `public."watchlist-signal-board"` — one
row per `run_date|symbol` (`signal_key`, unique), so same-day re-runs merge
safely instead of duplicating. `--skip-supabase` writes local files only.

Schema: `sql/watchlist_signal_board.sql` (idempotent DDL: table, indexes, RLS
enabled with no policies — service-role only, `updated_at` trigger). Re-create
from scratch with that file; same-day re-runs preserve `created_at` and touch
`updated_at`. `meta` jsonb carries all five upstream run dates; `directions`
jsonb keeps the per-signal vote audit trail; `scan_signals` jsonb keeps the
scan detail (crossovers, period return, SMA distances).

## Reading the board

- **Consensus** = majority of directional votes across the six signals
  (bull↑ / bear↓ / neut→). Sorted by net votes, then scan score.
- **CONFLICT flag** — bull and bear votes coexist. The board lists these
  separately; they are where a second look pays off most.
- **chart-veto flag** — jlaw-yahoo sees a setup but the chart review says
  Skip. Per the J Law doctrine, **a failing chart always overrides a
  flattering score** — treat these as no-trade until the chart improves.
- **flow-stale flag** — verdict older than 7 days; do not let it gate anything.
- Scan score bands: >6 strong trend, 4–6 moderate, 2–4 weak, <2 no trend
  (can go negative). It is a *trend confirmation* number, not a reversal or
  valuation signal — that is quant2's and stldc's job.
- quant2 rank is **not** a direction: it ranks weakness-repair LONG candidates,
  so a high quant2 rank on a bear-consensus row means "watch for reversal",
  not "buy now".

The full scoring rubric and every direction mapping live in
`references/methodology.md`. Read it before changing any gate or mapping.

## Example queries this skill serves

- "Scan the whole watchlist and compare with jlaw-v2-yahoo, jlaw-v2-chart, flow-verdict, quant2 and stldc"
- "Which watchlist names do all signals agree on today?"
- "Show me the conflicts on the signal board"
