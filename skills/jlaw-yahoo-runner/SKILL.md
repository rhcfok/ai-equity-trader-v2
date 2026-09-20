---
name: jlaw-yahoo-runner
description: Run and operate the deterministic Yahoo Finance OHLCV-based JLaw breakout screen without n8n. Use when an agent needs to screen a US-equity watchlist, calculate the original chart-based JLaw technical score from daily OHLCV data, create auditable JSON output, or optionally publish idempotent results to the existing Supabase table.
---

# JLaw Yahoo Runner

Use this skill to operate the Yahoo Finance-only replacement for **AI PM-Breakout-Jlaw**. The bundled runner implements **JLaw Yahoo OHLCV Model v2.0** and is deliberately aligned to the legacy chart rubric.

> Use daily **Yahoo Finance OHLCV** as the sole market-data source. Do not use ORATS, options data, option volume, open interest, implied volatility, chart images, vision models, or LLMs.

## Boundaries

- Screen verified US listings only.
- Treat daily OHLCV as the canonical basis for the score, price levels, and all legacy technical fields.
- Do not alter the 0–16 score with optional sentiment, earnings, options, or other external data.
- Do not place trades, connect a brokerage, or treat outputs as personalized advice.
- Keep Supabase secrets and `.env` files out of reports, logs, and source control.

## Bundled Resources

- `scripts/jlaw_yahoo_runner.py`: deterministic implementation.
- `scripts/requirements.txt`: Python dependency declaration.
- `templates/.env.example`: protected configuration template.
- `templates/watchlist.example.csv`: local US watchlist example.
- `references/yahoo-model.md`: signal definitions, data contract, and database behavior.

## Configuration

Yahoo Finance daily history requires no market-data key. Set Supabase variables only when reading the existing database watchlist or writing results.

| Variable | Needed for | Purpose |
|---|---|---|
| `YAHOO_FINANCE_BASE_URL` | Optional | Defaults to Yahoo Finance’s daily chart endpoint. |
| `SUPABASE_URL` | Database input/output | Supabase project or PostgREST URL. |
| `SUPABASE_KEY` | Database input/output | Key authorized for the watchlist and result tables. |
| `SUPABASE_WATCHLIST_TABLE` | Optional | Defaults to `watchlist`. |
| `SUPABASE_OUTPUT_TABLE` | Optional | Defaults to `n8n-breakout-jlaw`. |

Copy `templates/.env.example` into a protected `.env` file when needed. Process environment variables take precedence.

## Operating Workflow

1. **Select a US watchlist.** Use local CSV/JSON rows with a `symbol`/`ticker` and an exchange field, an explicit US symbol list, or the database watchlist.
2. **Validate before changing logic.** Run the unit tests before editing indicator or scoring behavior.
3. **Run a dry run first.** The default generates only a local JSON audit artifact. Check missing series, excluded listings, data timestamps, and scoring distribution.
4. **Check Yahoo history coverage.** Use the default two-year range; this must return at least 252 valid daily bars before scoring.
5. **Preserve the original rubric.** Compute SMAs, RSI, MACD, ADX, anchored quarterly VWAP, price/volume base structure, VCP, pocket pivot, and price levels only from Yahoo daily OHLCV.
6. **Require approval for external writes.** Add `--write-supabase` only after the operator explicitly approves result publication. The runner prevents duplicate same-date symbol rows.
7. **Schedule only after review.** Retain audit artifacts and logs in durable storage.

## Commands

Run tests from the runner package root:

```bash
python3 -m unittest discover -s tests -v
```

Run a local dry run:

```bash
python3 scripts/jlaw_yahoo_runner.py \
  --watchlist watchlist.csv \
  --output out/jlaw_$(date -u +%F).json
```

Run an ad hoc US symbol list:

```bash
python3 scripts/jlaw_yahoo_runner.py \
  --symbols AAPL,MSFT,SPY \
  --output out/ad_hoc.json
```

Publish only after explicit approval:

```bash
python3 scripts/jlaw_yahoo_runner.py \
  --watchlist watchlist.csv \
  --write-supabase \
  --output out/jlaw_$(date -u +%F).json
```

## Output Contract

| Group | Output |
|---|---|
| Score | `jlaw_score`, `jlaw_score_interpretation`, `rational` |
| Price levels | `current_price`, `pull_back_price`, `entry_price`, `stop_loss`, `target_price` |
| Legacy technical fields | `rsi`, `pct_from_52w_high`, `above_50ma`, `ma10_above_ma20`, `vcp_stage`, `pocket_pivot` |
| Audit metrics | SMAs, ADX, MACD histogram, anchored quarterly VWAP, equity volume, pivot price, base range, and bar count |

Use these interpretation bands without modification: **0–9 Skip**, **10–12 Watch / Small size**, **13–14 Valid trade**, and **15–16 Aggressive A+ setup**.

## Failure Handling

- Retry transient Yahoo Finance HTTP failures three times.
- Return an explicit unavailable-data result for missing or invalid series.
- Record any unresolved symbol failure in the audit file and return exit code `2` after processing the remaining symbols.
- Reject non-US or unverified listings before a Yahoo request.
- Do not use `--no-skip-existing` unless duplicates are explicitly intended.

*This is research and analysis only, not personalized financial advice.*
