# AI Equity Trader V2 — JLaw Yahoo Workflow

This repository contains the **deterministic JLaw Yahoo OHLCV Model v2.0** skill used by the AI Portfolio Manager's weekday breakout-screen workflow. The runner derives every score, price level, and technical label from daily Yahoo Finance OHLCV data only.

> This package is research tooling. It does not place trades or provide personalized financial advice.

## Included package

| Path | Purpose |
|---|---|
| `skills/jlaw-yahoo-runner/SKILL.md` | Operating boundaries, data contract, and workflow instructions. |
| `skills/jlaw-yahoo-runner/scripts/jlaw_yahoo_runner.py` | Deterministic Python runner. |
| `skills/jlaw-yahoo-runner/scripts/requirements.txt` | Minimal runtime dependency declaration. |
| `skills/jlaw-yahoo-runner/references/yahoo-model.md` | Technical scoring rubric and Yahoo data contract. |
| `skills/jlaw-yahoo-runner/templates/` | Sanitized environment-variable and US-watchlist examples. |

No production credentials, Supabase keys, live watchlists, historical audits, or result exports are included.

## Model constraints

- **Market data:** Yahoo Finance daily OHLCV (`/v8/finance/chart`) only.
- **History:** use the default `2y` range and require at least 252 valid daily bars.
- **Eligibility:** accept only supplied listings whose exchange identifies a verified US venue.
- **Scoring:** preserve the bundled 0–16 JLaw Yahoo OHLCV Model v2.0 without sentiment, option, news, vision, or LLM adjustments.
- **Exclusions:** do not use ORATS, options data, chart images, brokerage APIs, or order-execution functionality.

## Quick start

```bash
cd skills/jlaw-yahoo-runner
python3 -m pip install -r scripts/requirements.txt
python3 scripts/jlaw_yahoo_runner.py \
  --watchlist templates/watchlist.example.csv \
  --history-range 2y \
  --output out/jlaw_$(date -u +%F).json
```

The command creates a complete local JSON audit. Individual data failures are recorded in the audit and cause exit code `2` after the remaining symbols have been processed.

## Scheduled production pattern

The operating workflow keeps the runner's calculation independent from database access:

1. Read the current watchlist through an authorized database interface and save a local `symbol`/`exchange` JSON or CSV snapshot.
2. Run `jlaw_yahoo_runner.py` with that local file, `--history-range 2y`, and a timestamped JSON audit output path.
3. Inspect the audit's accepted listings, exclusions, unavailable or failed symbols, Yahoo `data_as_of` values, score distribution, and high-score names.
4. Before publication, query same-date existing symbols. Insert only candidates absent for the same date and symbol; never update or overwrite historical rows.
5. Retain the raw audit, a human-readable report, and a post-write count verification.

The runner also supports an explicit `--write-supabase` path for environments where a properly authorized Supabase REST key is provided. Keep `.env` files private and do not enable `--no-skip-existing` unless duplicate rows are expressly intended.

## Watchlist file format

```csv
symbol,exchange
AAPL,NASDAQ
MSFT,NASDAQ
SPY,NYSEARCA
```

JSON input is an array of equivalent objects. Symbols are normalized and deduplicated before processing.

## Disclaimer

This is research and analysis only, not personalized financial advice.
