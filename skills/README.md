# J Law v2 Pipelines

Two **independent, parallel** research pipelines applying J Law's (JLawStock)
trading doctrine to the Supabase `watchlist` table. Research and analysis
only — no orders, no brokerage connection, no personalized financial advice.

| | **jlaw-v2-yahoo** (quantitative) | **jlaw-v2-chart** (visual) |
|---|---|---|
| Universe | **Entire `watchlist` table** — all categories | `watchlist` where `category ∈ {core, satellite, watch1}` |
| Engine | `jlaw-yahoo-runner` (Yahoo OHLCV) + `jlaw_checklist.py` gate evaluator | TradingView "Kimi" layout screenshots, read against the chart-review checklist |
| Output | Score + doctrine gates → A+ / Valid / Watch / Skip | STRUCTURE / TRIGGER / STOP / ACTION per ticker |
| Supabase | `trade-jlaw-v2` rows with `jlaw_review_type = "data"` (**every** scored symbol, Skips included) | `trade-jlaw-v2` rows with `jlaw_review_type = "chart"` (**every** reviewed symbol, Skips included) |

Rows coexist under `unique (run_date, symbol, jlaw_review_type)` — same-day
reruns replace their own type's rows and never collide across pipelines.
Where both cover a symbol, **a failing chart overrides a flattering score**.

## Setup

Both pipelines need (env vars, never committed):

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL (project `vmxxmdtzvwizrpjrrvnp`) |
| `SUPABASE_KEY` | service-role key authorized for `watchlist` + `trade-jlaw-v2` |
| `SUPABASE_V2_TABLE` | optional, defaults to `trade-jlaw-v2` |
| `JLAW_REGIME` | `Risk-On` / `Neutral` / `Risk-Off` from the weekly MYT stance (yahoo pipeline) |

Additional machine requirements:

- **yahoo pipeline**: Python 3 stdlib only + network access to Yahoo Finance
  (via the sibling `jlaw-yahoo-runner` skill in this repo) and Supabase.
- **chart pipeline**: Python 3 stdlib only, plus **one** capture route:
  - Route A (preferred): TradingView **desktop app** logged into the user's
    account (cloud-synced "Kimi" layout), driven by a computer-use agent; or
  - Route B: Chrome with the Kimi WebBridge daemon at
    `http://127.0.0.1:10086/command`, chart layout id `hnR03PEX`.

## Run order (each pipeline, daily after US close)

**Yahoo pipeline** — full watchlist sweep:

```bash
# 1. universe: every symbol in watchlist (all categories)
#    select symbol from watchlist;   → save as watchlist_all.txt
python3 skills/jlaw-yahoo-runner/scripts/jlaw_yahoo_runner.py \
  --symbols "$(cat watchlist_all.txt)" --output out/jlaw_$(date -u +%F).json

# 2. doctrine gates (+ optional Supabase publication)
JLAW_REGIME=Risk-On python3 skills/jlaw-v2-yahoo/scripts/jlaw_checklist.py screen \
  --input out/jlaw_$(date -u +%F).json \
  --output out/jlaw_checklist_$(date -u +%F).json --write-supabase
```

**Chart pipeline** — core/satellite/watch1 visual review:

```bash
# 1. capture every chart in the universe (Route B shown; Route A is agent-driven)
python3 skills/jlaw-v2-chart/scripts/tv_capture.py \
  --from-watchlist --out tv_review_$(date -u +%F)

# 2. read each screenshot against
#    skills/jlaw-v2-chart/references/chart-review-checklist.md
#    → write chart_review_$(date -u +%F).json  (every reviewed symbol, once)

# 3. upsert all reviews
python3 skills/jlaw-v2-chart/scripts/chart_upsert.py \
  --input chart_review_$(date -u +%F).json
```

Run the Supabase upserts only when result publication is approved.

## Skill directories

- `jlaw-v2-yahoo/` — doctrine layer: methodology, official checklists,
  38+18 rules, daily routine, glossary + gate evaluator script
- `jlaw-v2-chart/` — visual layer: capture script, chart upsert script,
  chart-review checklist
- `jlaw-v2-check/` — QC layer: post-run audit of `trade-jlaw-v2`
  (coverage, labels, nulls, sanity, score-band, cross-pipeline agreement)
  + defect playbook. Run after every pipeline run:
  `python3 skills/jlaw-v2-check/scripts/jlaw_check.py`
- `jlaw-yahoo-runner/` — shared OHLCV screener used by the yahoo pipeline

Read each skill's `SKILL.md` first — it is the authoritative contract
(boundaries, verdict rules, output schemas, failure handling).
