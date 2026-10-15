# Screen policy and watchlist intake

## Default UW query

The runner embeds this configurable query in `DEFAULT_QUERY`:

```text
@stock where price >= 10 and marketcap >= 1B and avg30_stock_volume >= 500K and one_year_close > 0 and one_month_close > 0 and ((one_month_close / one_year_close) - 1) >= 10% and distance_to_sma_50 >= 0 and distance_to_sma_50 <= 8% and distance_to_sma_200 >= 0 and week_52_high > 0 and price >= week_52_high * 0.75
```

The API request also restricts `issue_types[]` to `Common Stock`.

| Condition | Purpose | JLaw relationship |
|---|---|---|
| Price ≥ $10 | Basic low-price/noise floor | Practical default; not a stated JLaw threshold |
| Market cap ≥ $1B | Keep initial list to liquid, established equities | Practical default; adjustable |
| 30-day average share volume ≥ 500K | Basic liquidity screen | Rejects many thinly traded names |
| 11-month return ≥ 10% | Measures return from the close ~1 year ago to the close ~1 month ago | Momentum proxy; not the JLaw RS-line test |
| Price above SMA50 and SMA200 | Require constructive trend location | A screen proxy; it does not verify that both averages are rising |
| No more than 8% above SMA50 | Avoid extended names | Matches the doctrine's distance cap |
| No more than 25% below 52-week high | Bias toward leaders near highs | Leadership proxy; not sector RS or a new-high requirement |

UW's `one_month_close` is the last regular close on or before one calendar month before the screener date; `one_year_close` is the corresponding close one calendar year before. Their ratio approximates 11-month momentum (excluding the latest month). This is a discovery heuristic, not a full quantitative-momentum model.

## What happens after screening

1. Request UW `Common Stock` results and page through the result set.
2. Accept only explicit U.S. exchange venues. If UW does not include a venue, use Yahoo Finance chart metadata; reject the symbol if venue resolution fails. Do not infer a U.S. listing solely from ticker format or a generic country label.
3. Read only `id,symbol,exchange,category` from Supabase `watchlist` and match symbols case-insensitively.
4. For new symbols, propose `add → watch1`.
5. For existing `core`, `satellite`, or `watch1` rows, propose `keep`. For existing lower-tier rows, propose `promote → watch1`. Preserve `hedge`/`hedges` rows.
6. Apply only `add` or `promote` actions when `--apply` is passed; never change unrelated fields. The default is read-only.
7. Leave downstream execution to existing scheduled/manual pipelines. The screener does not run chart review, Yahoo JLaw scoring, option-flow, Quant2, or GEX jobs itself.

## Interpretation boundaries

The screen can approximate trend location and momentum. It cannot reliably establish the visual quality of a VCP/base, volume dry-up into a pivot, an actionable pivot price, a rising 200-day SMA, or the sector's relative leadership. Use the existing Yahoo JLaw runner and TradingView chart checklist for those later checks. Do not pass UW data into or modify the existing JLaw 0–16 scoring logic.

API reference: [UW Stock Screener](https://api.unusualwhales.com/docs/operations/PublicApi.ScreenerController.stock_screener). The DSL uses `@stock`, `price`, `marketcap`, `avg30_stock_volume`, `distance_to_sma_50`, `distance_to_sma_200`, `week_52_high`, `one_month_close`, and `one_year_close`.
