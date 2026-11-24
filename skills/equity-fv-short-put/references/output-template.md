# Output template

Reproduce this structure. Keep prose to the minimum that carries the argument.

## Header

```
## <Basket description> — <tickers>
*Quotes captured <time/date>. Note any material intraday moves vs the prior close.*
```

If one finding reframes the whole basket (an earnings air-pocket, a cycle-peak discount, a liquidity
desert), put it in a short paragraph before the first table. The user reads that first.

## 1. Snapshot + fair value

```
| | Spot | TTM rev | TTM NI (margin) | FY+1E EPS | FY+1 P/E | Base FV | Bull | Bear | Prob-wtd | vs spot |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **TICK** | 000.00 | $00.0B | $0.00B (00%) | 0.00 | 00.0x | **000** | 000 | 000 | 000 | +0% |
```

- Bold the base FV.
- If a figure is derived rather than pulled, add a footnote directly under the table.
- `vs spot` uses the probability-weighted FV, not the base.

## 2. Per-name block

```
**TICK — $000.00.** Latest reported: revenue $00.0B (+00% YoY), gross margin 00%, net income $0.00B (00%).
TTM revenue $00.0B, net income $0.00B (00%). Street FY+1 $00.0B / EPS $0.00; FY+2 $00.0B / $0.00.
At $000 that is 00.0x FY+1 and 00.0x FY+2.

- **Bull $000 (00x FY+1):** <driver that has to happen — growth persists, margin expands, multiple re-rates>
- **Base $000 (00x FY+1):** <what the multiple assumes with no story>
- **Bear $000 (00x FY+1, EPS $0.00):** <what breaks — cycle rollover, pricing normalisation, share loss>

PW $000 (+0% vs spot).
**Jan 15 $000P at $0.00 bid**: 0.0% OTM, 00% below FV, 00% annualised, 00k OI, 0.0% spread. Earnings <date> —
<clean / event>. <One clause on why this strike or why no trade.>
```

The three cases are not optional. Each one is one line: the value, the multiple (and haircut EPS where the
bear differs), and the single operational driver. If you cannot name the driver, the case is not built.

For "no trade", replace the last sentence with the evidence:

```
**No put trade.** The 10–20% buffer strikes ($000–$000) have 00–00% bid-ask spreads and bids of $0.00–$0.00.
The only semi-liquid strike is 0% below FV with a 00% spread. Not investable.
```

## 3. Consolidated put table

Sort by conviction, not by ticker.

```
| Name | Exp | Strike | Bid | OTM | Buf @ K | Buf @ BE | Ann.% | Spread | OI | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| **TICK** | **Jan-15** | **000** | **0.00** | 0.0% | 00.0% | 00.0% | **00.0%** | 0.0% | 00k | One-line reason |
```

- `Ann.%` is bid-based.
- `Buf @ K` = strike vs base FV. `Buf @ BE` = breakeven vs base FV.
- Add a footnote: *Ann.% is bid-based; buffer @ BE uses breakeven (strike − credit), the number that
  matters if assigned.*
- Include "No trade" rows for names screened and rejected, with the reason.

## 4. Risk notes

Five or six numbered bullets, each one or two sentences:

1. **Correlation.** Group the names into factors (AI capex, aerospace cycle, rates) and say how many
   independent bets the list really is.
2. **The live revision risk.** Which consensus number is most likely to move, and in which direction.
3. **Earnings timing.** Which expiries are clean, which capture prints.
4. **Liquidity.** Name the names where the quotes are not executable at size.
5. **Data caveats.** Stale spots, derived figures, wrong-listing fallbacks, one-off distortions.
6. **What would change the call.** One falsifiable signpost per major conclusion if space allows.

## 5. Ranking

```
**Ranking:** TICK (reason) → TICK (reason) → TICK (reason) → no trade: TICK, TICK.
```

## Style

- Numbers first. No "let's dive in", no restating the request.
- Use "Street consensus" / "consensus embeds", never invented management guidance.
- Bold only the decision variables: base FV, recommended strike, recommended annualised return.
- One caveat per sentence; do not stack three hedges into a paragraph.
- If a name has no trade, say it plainly and move on.
