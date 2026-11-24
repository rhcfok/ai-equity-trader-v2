---
name: equity-fv-short-put
description: Build a ticker-level equity fair-value and short-put trade sheet. Use whenever the user asks for a valuation plus cash-secured/short-put ideas on a stock or basket — phrasings like "do the same analysis for X", "what's the FV", "find puts 10-20% below fair value", "SP candidates", "short put screen", "how about MRVL?", or a bare list of tickers. Produces a live snapshot, TTM financials, forward consensus, bull/base/bear and probability-weighted fair value, and a 10-20% FV-buffer put table with liquidity and earnings-timing checks.
---

# Equity FV → short-put trade sheet

This skill replicates one repeatable workflow: for each ticker, establish what the business earns now,
what the Street expects next, a defensible fair value with a bull/base/bear range, and then the
short-put strikes that sit 10–20% below that fair value — screened for real liquidity.

The output is a trade sheet, not a research report. Be compact, quantitative, and willing to say
"no trade" when the options market doesn't pay for the risk.

## Quick start

```
1. Parse tickers; cap at 9. Budget Alpha Vantage: 2 calls/name.
2. Wind price indicators for all names (parallel) -> spot, mkt cap, TTM/forward P/E, 52w range.
3. Alpha Vantage INCOME_STATEMENT (or Wind TTM fallback) -> TTM revenue, NI, margins.
4. Alpha Vantage EARNINGS_ESTIMATES -> FY+1 and FY+2 revenue/EPS. Check the EPS trajectory.
5. Build bull/base/bear + probability-weighted FV (scripts/fv_math.py helps).
6. Write names.json {TICKER: FV}; run scripts/uw_put_screen.py.
7. Select strikes 10-20% below FV; reject bid<$0.50 / spread>25% / OI<200; check earnings timing.
8. Write the sheet per references/output-template.md.
```

## Inputs and batch size

- Parse the ticker list from the request. Accept `$MSFT`, `MSFT`, `Microsoft`, and mixed lists.
- **Cap a batch at 9 names.** Alpha Vantage's free tier allows 25 requests/day and each name costs up
  to 2 (income statement + estimates). Nine names leaves a buffer for retries. If the user lists more,
  split into batches and say so.
- If the user says "the same analysis" without naming a name, use the most recently discussed ticker.

## Stage 0 — resolve and budget

Map US tickers to Wind codes: Nasdaq → `.O`, NYSE → `.N`. After the first Wind call, check the returned
`Wind代码` matches. Wind sometimes resolves a US ticker to a foreign listing (e.g. `RTX.N` came back as
`0NQJ.L` in DKK). If the listing or currency is wrong, discard that pull and use Alpha Vantage instead.

Budget Alpha Vantage before calling: `2 × names` requests. If the quota is short, prioritise
`EARNINGS_ESTIMATES` (forward EPS is non-negotiable for valuation) and take TTM financials from Wind.
Never invent a consensus number. If a pull fails, label the figure "derived" and show the arithmetic.

## Stage 1 — snapshot

Use the Wind global-stock price tool (`get_global_stock_price_indicators`) with:
`最新交易日,最新成交价,前收盘价,总市值1,市盈率(TTM),市盈率(预测),52周最高,52周最低,年初涨跌幅`.

Record: last trade date, price, prior close, market cap, TTM P/E, forward P/E, 52-week high/low, YTD.
The 52-week range is diagnostic: a name 50% off its high is being priced as a cycle peak, which changes
how you read the multiple.

## Stage 2 — current financials

Preferred source: Alpha Vantage `INCOME_STATEMENT`. Read the two most recent quarterly rows and sum the
last four for TTM revenue / net income / gross margin. Compute margins yourself; don't trust a single
vendor field.

Fallback: Wind `get_global_stock_fundamentals`. Two hard rules:

1. **Units are 亿元 (100 million).** Multiply by 0.1 to get $B. A figure of `97.31` is $9.73B, not $97B.
2. **"最新营业收入" is period-ambiguous** — it can be a quarter, a fiscal year, or YTD. Only the `_TTM`
   fields are safe to compare across names. Cross-check: TTM P/E should equal market cap ÷ TTM net income.
   If it doesn't, one of the two is wrong; trust the P/E identity and re-derive.

Also capture the latest reported quarter's revenue growth (YoY and QoQ) when the source is clean. If it
isn't, present TTM only and say so — a wrong "latest quarter" line is worse than none.

## Stage 3 — forward consensus

Call Alpha Vantage `EARNINGS_ESTIMATES`. Take the two fiscal-year rows (not quarters). Record revenue and
EPS for FY+1 and FY+2, plus the analyst count.

**Read the EPS trajectory before computing any multiple.** If FY+1 EPS is below FY0, the trailing P/E is
flattering and the name is more expensive than it looks. This single check has repeatedly inverted the
conclusion (GOOGL looked 14x trailing but was 19x on FY27 with EPS down 24%; AMZN 20x vs 24x with EPS
down 17%). Call it out explicitly in the write-up — it is usually the most valuable sentence in the sheet.

When AV quota is exhausted:
- Wind's `市盈率(预测)` gives a forward P/E. Implied forward EPS = price ÷ forward P/E. Label it derived.
- For a name with clean quarterly reports, annualise the last quarter's net income and grow it at the
  recent YoY rate. Label the growth assumption.

## Stage 4 — fair value

Use three anchors and show the multiple you applied:

1. **P/E on forward EPS** (primary). Pick the year — FY+1 for stable businesses, FY+2 for名字 where
   FY+1 is distorted by capex or a cyclical dip. A multiple is only defensible with a reason: growth rate,
   margin structure, ROIC, and where the stock has traded historically.
2. **EV/sales** cross-check. EV = market cap + net debt − cash. For high-growth names where earnings are
   depressed, sales is the cleaner anchor.
3. **FCF yield / reverse DCF** cross-check where the company is FCF-positive.

Then set the range:
- **Base**: the multiple you would defend with no narrative.
- **Bull**: base multiple + 3–5 turns, or the multiple implied if growth persists two more years.
- **Bear**: a de-rating multiple (often 20–35% below base) applied to hair-cut EPS, not just a lower multiple.
  State what breaks — cycle rollover, share loss, pricing normalisation, capex digestion.

Probability-weight: 25/50/25 for balanced names, 20/50/30 toward the bear for high-multiple or
cyclical names. Report probability-weighted FV and the premium/discount to spot.

### Special cases

- **Stock above base FV (rich).** The 10–20% FV buffer sits 20–40% below spot. Premiums are thin and
  annualised returns often fall under 10%. Say "no trade" rather than selling a 3-delta lottery ticket.
- **Stock below base FV (cheap).** A strict 10–20% FV buffer implies strikes at or above spot (ITM puts).
  Present the closest liquid OTM strike and note it gives *more* cushion than requested, plus the ITM
  alternative framed as "synthetic long at a discount to FV".
- **Financials distorted by a one-off.** Use operating income or adjusted EPS and say which. A 50% net
  margin at a 30%-margin business is a red flag, not a bargain.

## Stage 5 — short-put screen

Run the bundled script (paths are relative to this skill's directory):

```
python -I <skill-dir>/scripts/uw_put_screen.py --config names.json --out uw_out
```

`names.json` is `{"TICKER": fair_value, ...}`. The script finds the UW key without printing it, pulls
company info, next earnings and four option chains per name, and prints candidates in the FV band.
Default band: `0.72×FV ≤ strike ≤ min(0.95×FV, 0.995×spot)`, OI ≥ 50. Override with `--band-low`,
`--band-high`, `--spot-cap`, `--min-oi`, `--expiries`.

If the script can't find the key, stop and report BLOCKED. Do not fabricate option quotes.

The script outputs bid, mid, IV, delta, OI, spread, annualised return, breakeven, buffer at strike, buffer
at breakeven, and OTM%. **Use the bid-based annualised return in the write-up** — the script's `ann` column
is mid-based because it sorts on mid. Recompute at bid: `bid / strike × 365 / DTE`.

### Selection rules

- Prefer strikes whose **buffer at the strike** is 10–20% below base FV. Buffer at breakeven is the
  secondary number; report both.
- Reject a strike if any of: bid < $0.50, spread > 25%, OI < 200 (unless nothing better exists and you
  flag it), or annualised bid return < 10% for a name with IV > 40%.
- Check the earnings date against the expiry. An expiry after the print carries event gap risk; an expiry
  before it is "clean". Say which. Prefer the clean expiry when the premium difference is small.
- Note when the UW live spot differs materially (>2%) from the prior close; quotes move intraday.

## Stage 6 — write the sheet

Follow `references/output-template.md`. Structure:

1. One-paragraph framing: what the basket has in common and the single most important finding.
2. Snapshot + FV table (spot, TTM revenue/NI/margin, FY+1 EPS, FY+1 P/E, base/bull/bear, prob-weighted,
   vs spot).
3. Per-name block: 2–4 sentences of current financials and the forward trajectory, then the
   **bull/base/bear case block** — one line per case with the value, the multiple, the bear EPS haircut
   where applicable, and the single operational driver. Then the recommended put (or "no trade" with the
   reason). A case without a named driver is not built.
4. Consolidated put table, sorted by conviction, with a verdict column.
5. Risk notes: correlation, the live revision risk, earnings timing, liquidity, and data caveats.

End with a ranking. The user reads the ranking first on repeat requests.

## Guardrails

- Never present management guidance you did not retrieve. Say "Street consensus" or "consensus embeds".
- Label derived numbers as derived and show the arithmetic in one clause.
- Do not print or persist API keys. The script reads them from the env file and never echoes them.
- If a data source is stale, wrong-listing, or unit-ambiguous, say so in the sheet rather than smoothing it over.
- Keep the tone of a desk note: numbers first, caveats second, no filler.

## References

- `references/methodology.md` — multiple-selection heuristics by business type, the pitfalls log, and
  worked examples of the FV math.
- `references/output-template.md` — the literal table and block format to reproduce.
- `scripts/uw_put_screen.py` — the UW MCP chain scanner.
- `scripts/fv_math.py` — P/E, EV/sales, probability-weighted FV and put-economics helpers.
