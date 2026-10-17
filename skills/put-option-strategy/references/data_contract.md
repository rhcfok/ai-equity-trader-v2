# CSP / BPS Data Contract — Ranked Watch v3

Read before changing source mappings, hard gates, scoring, calculations, or ledger behavior.

## Sources and hierarchy

| Input | Source | Required fields | Role |
|---|---|---|---|
| Scope | `watchlist` | `symbol`, `exchange`, `category` | Explicitly US-listed names in requested categories only. |
| Direction / stop | `trade-jlaw-v2` | `symbol`, `run_date`, `classification`, `jlaw_score`, `current_price`, `stop_loss`, IV fields | Latest row; prefer chart review. **Valid and Watch are allowed.** Skip is excluded. |
| Stored GEX | `gex_snapshot` | `symbol`, `snapshot_ts`, `put_wall`, `call_wall`, `regime`, `net_gex_mm` | Safety-floor input only when its regime is valid. |
| Quant confirmation | `quant2_leaderboard` | `ticker`, `as_of`, `rank`, `score_hist_freq`, `n_active_rules`, `active_rules` | Corroborative ranking input only. |
| Option mechanics | Unusual Whales option contracts / `get_options_chain` | OCC symbol, delta, OI, volume, NBBO bid/ask, IV, timestamp | Canonical contract/quote source. |
| Earnings firewall | Unusual Whales upcoming earnings | ticker, report date/time, expected move | Confirm event is strictly after expiry. |
| Live GEX | Unusual Whales `get_gex_levels(source="oi")` | put wall, call wall, gamma flip, date | Cross-check market structure; volume GEX is intraday context only. |
| Volatility curve | Unusual Whales IV term structure | expiry, DTE, IV, implied move | Classify normal versus event-distorted premium. |
| Flow / OI review | Unusual Whales flow by strike, OI changes, alerts, multi-leg flow | timestamps, strike, volume/OI, bid/ask-side fields | Ranking caution only; no volume-as-intent assumption. |

## Structural hard gates

### Underlying scope

```text
explicit US exchange
AND requested watchlist category
AND latest J Law classification in {Valid, Watch}
AND positive J Law stop loss
```

`Watch` is an eligible underlying label. It is visible in the board and is not rejected merely for not being `Valid`.

### Safety floor

```text
if GEX put wall exists and regime is neither sub_resolution nor unknown:
    safety_floor = min(jlaw.stop_loss, gex.put_wall)
else:
    safety_floor = jlaw.stop_loss
```

Treat `sub_resolution` GEX as map-only. Do not use its wall to tighten the floor or award GEX score points.

### CSP

```text
3 <= calendar DTE <= 10
AND put strike < spot
AND signed short-put delta < -0.20
AND short strike <= safety_floor
AND OI >= configured minimum
AND NBBO width / midpoint <= configured maximum
AND confirmed earnings date > expiry
```

### 10-point BPS

```text
7 <= calendar DTE <= 30
AND short put is OTM
AND short strike <= safety_floor
AND same-expiry long strike = short strike - 10.00
AND both legs meet OI and NBBO gates
AND short bid - long ask > 0
AND 20% <= net credit / maximum loss <= 30%
AND confirmed earnings date > expiry
```

BPS delta is display-only: it is neither a gate nor a score input.

## Economics

Quotes and strikes are per share; reported economics are one 100-share contract.

```text
CSP credit                 = short bid × 100
CSP collateral             = (short strike − short bid) × 100
CSP credit / collateral    = CSP credit ÷ CSP collateral

BPS net credit             = (short bid − long ask) × 100
BPS maximum loss           = (10.00 × 100) − BPS net credit
BPS return on risk         = BPS net credit ÷ BPS maximum loss
BPS break-even             = short strike − (BPS net credit ÷ 100)
```

Reject a quote if bid or ask is absent/non-positive, ask is lower than bid, or width/midpoint exceeds the cap. Never substitute last or theoretical price.

## Confidence score and Top-20 policy

The 0–100 **Ranked Confidence Score** is a deterministic ordering device, not a probability of profit, probability of assignment, expected P&L, position-size instruction, or order recommendation.

| Pillar | Maximum points |
|---|---:|
| J Law / technical safety | 20 |
| Quant2 corroboration | 20 |
| GEX map quality | 15 |
| Volatility / event quality | 15 |
| Liquidity / execution | 10 |
| Reward quality | 10 |
| Flow / OI review | 10 |

1. Apply every structural hard gate first.
2. Score **every** hard-pass CSP and BPS structure, including scores below 70.
3. Sort all eligible structures across both strategies by score, reward metric, strategy, symbol, and expiry.
4. Publish the first 20 rows (or all rows if fewer than 20 pass). Do not use a score threshold to remove a hard-pass candidate.
5. Use score bands only for interpretation: 85–100 Strong screen; 70–84 Higher screen; 60–69 Moderate screen; below 60 Lower screen.
6. Adverse near-strike flow caps score at 69 and is a caution flag; it does not independently remove a structurally eligible candidate.

## Ledger and end-of-day tracking

`public."csp-bps-opportunity"` is a research-tracking ledger, not an executed-trade ledger.

- After writing the live screen's report and audit, automatically upsert the rows in `ranked_top20`; do not pause for manual report review.
- Log at most 20 hard-pass rows total across CSP and BPS, ordered by `confidence_score` descending, `reward_metric` descending, strategy, symbol, and expiry. Do not apply a minimum score cutoff; lower score bands remain eligible when every structural gate passes. Log all candidates if fewer than 20 pass.
- Store each row as `ELIGIBLE` and retain its `top20_rank` in the audit payload. The logger consumes the runner's ordered `ranked_top20` array; no schema change is required.
- Use the existing `opportunity_key` upsert and do not delete prior ledger records or alter source tables. A live run is incomplete if the write fails, even if its report artifacts were generated.
- A screen with no structural hard-pass row logs one `NO_QUALIFIED_SETUP` record, excluded from the EOD win-rate denominator.
- Mark same-day hypothetical exits conservatively: CSP short ask; BPS short ask less long bid.
- For calibration, retain expiry close, short-strike/break-even moneyness, assignment proxy, and hypothetical expiry P&L. Do not label these as executed-trade outcomes.

## Key reason codes

- `JLAW_CLASSIFICATION_NOT_ALLOWED`
- `CSP_DELTA_NOT_STRICTLY_BELOW_THRESHOLD`
- `ABOVE_TECHNICAL_SAFETY_FLOOR`
- `EARNINGS_OVERLAP` / `EARNINGS_UNCONFIRMED`
- `OPEN_INTEREST_BELOW_MINIMUM`
- `NBBO_QUOTE_MISSING` / `BID_ASK_SPREAD_TOO_WIDE`
- `DEFAULT_WIDTH_LONG_PUT_MISSING`
- `BPS_RETURN_ON_RISK_OUTSIDE_20_TO_30_PCT_BAND`

## Sources

- [Unusual Whales option contracts](https://api.unusualwhales.com/docs/operations/PublicApi.OptionContractController.option_contracts)
- [Unusual Whales option screener](https://api.unusualwhales.com/docs/operations/PublicApi.ScreenerController.contract_screener)
- [Unusual Whales flow alerts](https://api.unusualwhales.com/docs/operations/PublicApi.OptionTradeController.flow_alerts)
