# Methodology and pitfalls

## 1. Choosing the multiple

A fair value is only as good as the multiple you apply. Anchor the multiple to the business model and
the point in the cycle, and say which year's EPS you are capitalising.

| Business type | Primary anchor | Typical base multiple | Cross-check |
|---|---|---|---|
| Mega-cap platform (MSFT, GOOGL, AMZN, META) | FY+1 P/E | 20–30x | EV/sales 5–9x |
| High-growth software (CRWD, DDOG, SNOW, NET, NOW) | FY+2 P/E or DCF | 25–45x | EV/sales 6–12x |
| Mature software (CRM, ADBE, ORCL) | FY+1 P/E | 18–28x | EV/FCF 20–30x |
| AI semis (NVDA, AVGO, MRVL) | FY+2 P/E | 12–20x | FY+2 EV/sales 6–10x |
| Semi equipment (AMAT, LRCX, KLAC) | FY+1 P/E | 15–20x mid-cycle, 10–14x at peak | EV/sales 4–7x |
| Analog semis (ADI, TXN) | FY+1 P/E | 22–30x | EV/sales 6–9x |
| IP/licensing (ARM) | FY+2 P/E or EV/sales | 60–100x forward EPS, 20–30x sales | margin-expansion DCF |
| Aerospace aftermarket (GE, HWM, TDG) | FY+1 P/E | 28–35x | EV/EBITDA 18–24x |
| Defense prime (RTX, LMT, NOC) | FY+1 P/E | 18–25x | FCF yield 4–6% |
| Storage/HDD (STX, WDC) | FY+1 P/E at mid-cycle | 8–13x | EV/EBITDA 5–8x |
| Small-cap software (FROG, DOCN, ESTC) | DCF or EV/sales | 4–8x sales | Rule of 40 |

Two adjustments dominate:
- **Growth**: a 20%-grower deserves roughly 1.0–1.5x the growth rate in P/E (PEG 1.0–1.5). A 50%-grower
  can hold 40–60x if margins are expanding.
- **Cyclicality**: when margins are at a record, the market applies a *lower* multiple to peak EPS.
  A 57%-gross-margin HDD maker at 12x peak EPS is not cheap if EPS halves next year. Capitalise
  mid-cycle EPS, not peak EPS.

## 2. The FY+1 EPS trajectory check

Before computing any multiple, compare FY+1 consensus EPS to FY0. If FY+1 is below FY0:

- The trailing P/E understates the true multiple. Recompute on FY+1.
- Ask why. Common causes: capex depreciation (AI build-out), cyclical margin rollover, a one-off gain
  in FY0, or a step-up in R&D.
- The write-up should lead with this. Example from the Nov-2026 batch: GOOGL traded at 14.3x FY26 but
  18.8x FY27 because FY27 EPS consensus was −24% (capex depreciation plus search monetisation). The
  "cheap" headline was wrong.

Conversely, if FY+1 EPS is far above FY0 (NVDA +72%, STX +63%, LITE +61%), the trailing multiple
overstates the expense — but check whether the growth is capital-cycle-driven.

## 3. Detecting distorted financials

Three quick checks:

1. **Net margin vs operating margin.** A net margin far above the operating margin means non-operating
   income (interest, gains, tax benefits). GOOGL's 50% TTM net margin on a ~35% operating business is a
   red flag; LITE's −$7.2B quarter is a non-cash charge.
2. **TTM vs fiscal year.** If TTM net income is far above the last fiscal year, a one-off landed in the
   TTM window. MSFT's TTM NI of $142.6B vs FY26 ~$117B is the tell.
3. **P/E identity.** Market cap ÷ TTM net income must equal the vendor's TTM P/E. If not, one input is on
   a different period or currency. Trust the identity and re-derive the missing piece.

When distorted, value off operating income or adjusted EPS and say which.

## 4. Constructing the range

- **Base**: the multiple you would defend with no story. Apply it to the EPS year that best represents
  normal earnings power (FY+1 normally; FY+2 when FY+1 is a capex trough).
- **Bull**: base multiple +3–5 turns, or the multiple implied if growth persists two more years. Bull EPS
  can be above consensus only if you can name the driver.
- **Bear**: haircut *both* the multiple and the EPS. A bear case that only cuts the multiple to 18x on
  unchanged peak EPS is not a bear case. State the operational break: cycle rollover, pricing
  normalisation, share loss, capex digestion, regulatory remedy.
- **Weights**: 25/50/25 balanced; 20/50/30 toward the bear for high-multiple or cyclical names; 30/50/20
  toward the bull only when the balance sheet and backlog de-risk the downside.

Report probability-weighted FV and the premium/discount to spot. If the PW value is within ±5% of spot,
say the stock is fairly valued — that is a finding, not a failure.

## 5. The 10–20% buffer, precisely

The user's phrase "10–20% buffer on fair value" means the strike sits at 80–90% of FV:

```
strike ∈ [0.80 × FV, 0.90 × FV]
```

Screen a wider band (0.72–0.95 × FV) and then select within the strict band.

Edge cases:
- **Spot below 0.90 × FV (stock cheap).** The strict band is at or above spot → ITM puts. Present the
  nearest liquid OTM strike, note it gives more than the requested cushion, and frame the ITM strike as
  "synthetic long at a discount to FV" only if the user wants to own the stock.
- **Spot above FV (stock rich).** The strict band is 20–40% OTM. Premiums collapse and annualised returns
  often fall below 10%. Say "no trade" rather than selling a low-delta tail.
- **Illiquid chain.** If bid < $0.50, spread > 25%, or OI < 200, the headline yield is not executable.
  Say so and either size down or pass.

Always report both **buffer at the strike** and **buffer at breakeven** (strike − credit). The second is
the economic cushion if assigned.

## 6. Option economics

```
static_return = bid / strike
annualised    = static_return × 365 / DTE
breakeven     = strike − bid
collateral    = strike × 100 per contract
```

Use the **bid**, not the mid. Mid-based annualised returns overstate what you can actually fill,
especially where spreads are 15–40%. If you quote mid anywhere, label it.

Probability of profit is approximately `1 − |delta|` under the risk-neutral measure. Treat delta as a
rough ITM probability, not a precise POP.

## 7. Pitfalls log

- **Alpha Vantage free tier is 25 requests/day.** Each name costs up to 2 calls. The rate-limit error
  echoes the API key — do not repeat it in the response. Budget before starting; if short, prioritise
  `EARNINGS_ESTIMATES` and use Wind TTM for financials.
- **Wind units are 亿元 (100 million).** Multiply by 0.1 for $B. A value of `97.31` is $9.73B.
- **Wind's "最新营业收入" is period-ambiguous** — quarter, fiscal year, or YTD depending on the name.
  Only `_TTM` fields are comparable across names.
- **Wind occasionally resolves a US ticker to a foreign listing.** `RTX.N` returned `0NQJ.L` in DKK.
  Check the returned code and currency; discard mismatches.
- **UW's live spot can differ materially from the prior close.** On Nov 24, 2026, ADI was −11% into its
  print, AMAT −4.7% after +14.6% the prior day. Quotes move; re-pull before ordering.
- **UW chain `price_data.price` can differ from `get_company_info.price`.** Prefer the chain's price for
  that expiry and say which timestamp you used.
- **Earnings inside the expiry.** Check every expiry against the next report date. An expiry after the
  print carries gap risk; one before it is clean. Prefer clean when the premium difference is small.
- **Thin chains.** Industrial and small-cap puts often show OI in the hundreds and spreads of 70–200%.
  HWM's 10–20% buffer strikes had bids of $0.05–$1.35 and 70–120% spreads — untradeable.
- **Never print or persist the UW key.** The bundled script reads it from an env file and does not echo it.
- **Do not fabricate guidance.** If you did not retrieve management guidance, write "Street consensus" or
  "consensus embeds". Do not attribute numbers to management.

## 8. Worked examples

**GOOGL, Nov 2026.** Spot $279.50. FY26E EPS $19.54, FY27E $14.89 (−24%). A 18.8x multiple on FY27 gives
$280. Bull 22x = $330, bear 14x = $210. PW (25/50/25) = $275. The trailing 14.3x was an artefact of
inflated FY26 earnings. The put at $250 was 10.7% below FV and paid 12.9% annualised — fair, not exciting.

**AMAT, Nov 2026.** Spot $330.42 after a 55% drawdown from $737.77. FY27E EPS $19.30, FY28E $24.63. Base
20x FY27 = $385; bull 24x = $460; bear 13x on $14 EPS = $250. PW = $370 (+12%). The Jan $320 put was 3.2%
OTM, 16.9% below FV, and paid 46% annualised — the best carry in the batch, but with 1.1k OI and 68% IV.

**HWM, Nov 2026.** Spot $217.20, FY27E EPS $6.65, base 30.8x = $205, PW $200 (−8%). The stock was rich,
and the 10–20% buffer strikes had 70–120% spreads. Correct output: no trade, with the spread evidence.
