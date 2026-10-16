---
name: put-option-strategy
description: "Screen US-listed watchlist names for research-only cash-secured puts and 10-point bull put spreads using J Law, Quant2, GEX, Unusual Whales chains, earnings, IV, and option-flow context. Use when producing a ranked Top-20 put-option opportunity board, validating CSP 3–10 DTE delta below -0.20, validating BPS 7–30 DTE with 20–30% return on risk, or logging hypothetical EOD outcomes without creating orders."
---

# Ranked CSP / 10-Point BPS Strategy

Use this skill for a **research-only** US-listed put-option board. It produces an auditable Top-20 ranking and can persist research records to `public."csp-bps-opportunity"`. It never creates an order or an order instruction.

Read `references/data_contract.md` before changing data mappings, gates, scores, calculations, or ledger behavior.

## Mandatory policy

| Strategy | DTE | Delta | Economics | Structure |
|---|---:|---|---|---|
| CSP | 3–10 calendar days | Signed short-put delta strictly `< -0.20` | Rank short-bid credit / collateral after safety gates | Sell one OTM put |
| BPS | 7–30 calendar days | Display only; no gate or score | 20%–30% return on risk | Sell one OTM put and buy a same-expiry put exactly 10 points lower |

Apply without exception:

1. Screen only requested **US-listed** `watchlist` symbols.
2. Allow the latest J Law label **Valid or Watch**. Exclude Skip.
3. Require a positive J Law stop and an OTM short strike at or below the applicable safety floor.
4. Use `min(J Law stop, OI-GEX put wall)` only for a valid GEX regime. Label `sub_resolution` GEX as map-only.
5. Require earnings strictly after option expiry and usable NBBO / OI gates on all required legs.
6. For BPS, require exact 10-point width, positive net credit, and 20%–30% return on risk.
7. Score all mechanical hard-pass structures. **Do not filter on score.** Rank all eligible CSP/BPS structures together and publish the first 20.

## Ranked confidence score

The 0–100 score is an interpretable sorting field only:

- **85–100:** Strong screen
- **70–84:** Higher screen
- **60–69:** Moderate screen
- **Below 60:** Lower screen

An adverse near-strike flow observation caps the score at 69 and must be shown as a caution; option volume and same-day OI are not proof of opening directional intent.

## Workflow

1. Anchor the report to the most recent completed US market session. Label all source dates and quote timestamps.
2. Resolve requested US watchlist names and their latest J Law, Quant2, and stored GEX context. Retain Valid and Watch; record Skip/non-US exclusions in the audit.
3. Pull the canonical Unusual Whales contract chain for each surviving ticker. A broad screener is discovery-only and cannot establish absence of contracts.
4. Retrieve earnings, OI GEX, IV term structure, and flow/OI context. Treat earnings overlap as a hard reject and flow/OI as a scored caution only.
5. Construct CSPs and 10-point BPSs from NBBO; use short bid and long ask, never last or theoretical prices.
6. Apply mechanical hard gates. Score every pass, then rank the combined CSP/BPS population to the configured Top N (default 20).
7. Produce Markdown report plus immutable JSON audit. State key reasons and cautions, including whether quotes are live, delayed, frozen, or close-of-session.
8. Persist Top-20 `ELIGIBLE` research rows only after the report has been reviewed. If there are no hard-pass rows, log `NO_QUALIFIED_SETUP`.
9. For same-day tracking, use conservative close-to-exit marks and label results hypothetical, not executed P&L.

## Commands

Run regression tests after code changes:

```bash
cd scripts
python3 -m unittest -v test_csp_bps_opportunity_runner.py test_csp_bps_opportunity_tracking.py
```

Run the fixture-driven deterministic screen:

```bash
python3 scripts/csp_bps_opportunity_runner.py \
  --fixture /secure/put_option_fixture.json \
  --as-of YYYY-MM-DD \
  --top-n 20 \
  --output-dir /home/ubuntu/output/put_option_YYYY-MM-DD
```

The default accepts Watch. Use `--valid-only` only when intentionally restricting the universe.

Log the board after review:

```bash
python3 scripts/csp_bps_opportunity_log_runner.py log \
  --audit /home/ubuntu/output/put_option_YYYY-MM-DD/csp_bps_opportunity_audit.json \
  --report /home/ubuntu/output/put_option_YYYY-MM-DD/csp_bps_opportunity_report.md
```

## Output requirements

- Show rank, score/band, strategy, ticker/J Law label, expiry/DTE, contract symbols, NBBO economics, break-even, and concise source context.
- Keep complete score components, source context, reason codes, and every rejection in the audit JSON.
- Never call a ranking score a probability, forecast, or personalized recommendation.
- Never treat stale, frozen, missing, last-only, or theoretical quotes as executable.

This is research and analysis only, not personalized financial advice.
