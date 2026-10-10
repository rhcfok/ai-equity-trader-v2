# GEX methodology (unsigned)

## Source

Unusual Whales `greek-exposure/expiry` provides the DTE calendar only. The calculation uses paginated `option-contracts` rows: per-contract gamma, open interest, strike, and option type. Underlying spot comes from the provider quote endpoint.

## Filters

- DTE window: 0–30 by default.
- Nearest-DTE expiry only per strike; never sum expiries.
- Min OI: 50 contracts on at least one side (configurable).
- Moneyness: ±20% of spot (configurable).
- Sub-resolution: `|net_gex_mm| < 1.0` → `sub_resolution`.
- Walls must lie on the correct side of spot: call ≥ spot, put ≤ spot.

## Formula

Per side, per strike ($MM for a 1% move):

```
gex_side_$MM = gamma × oi × 100 × spot × 0.01 / 1e6
```

Put-side contribution is stored negative in the ladder/net arithmetic. Call- and put-side source quantities remain unsigned magnitudes.

## Regime / gravity

- `positive` if net call-side magnitude minus put-side magnitude is ≥ resolution.
- `negative` if it is ≤ −resolution.
- `sub_resolution` otherwise.
- Gravity is `up` when the eligible call wall dominates, `down` when the eligible put wall dominates, otherwise `n/a`.

## Explicit non-claims

- Do not infer dealer long/short gamma from option open interest.
- Do not use provider signed/assumed GEX fields as dealer-position evidence.
- `zero_gamma` is null in this pipeline because it needs a signed source.
- Briefings must describe results as **unsigned** call/put-side GEX magnitude.
