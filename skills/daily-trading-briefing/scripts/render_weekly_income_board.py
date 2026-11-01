#!/usr/bin/env python3
"""Validate and render a compact, review-only weekly options-income board.

The input manifest must be assembled from timestamped market-data sources. This
script never fetches data, writes a database, submits orders, or represents
historical quotes as live quotes.
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


SINGLE_PUT_STRATEGIES = {"SP", "CSP", "WHEEL_CSP"}
SPREAD_STRATEGIES = {"BPS", "BCS"}
ALLOWED_STRATEGIES = SINGLE_PUT_STRATEGIES | SPREAD_STRATEGIES


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def row(values: list[Any]) -> str:
    return "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in values) + "</tr>"


def money(value: float | int) -> str:
    return f"${float(value):,.0f}"


def n(value: Any) -> float:
    return float(value)


def maybe_count(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "n/a"


def validate(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rules = manifest.get("board_rule", {})
    board = manifest.get("board")
    if not isinstance(board, list) or not board:
        raise ValueError("manifest.board must be a non-empty list")

    total = int(rules.get("total_contracts", 20))
    max_per_ticker = int(rules.get("max_contracts_per_ticker", 2))
    csp_cap = int(rules.get("csp_dte_cap", 35))
    spread_cap = int(rules.get("spread_dte_cap", 45))
    single_target = int(rules.get("single_put_contracts", total // 2))
    spread_target = int(rules.get("spread_contracts", total - single_target))
    if total != single_target + spread_target:
        raise ValueError("single_put_contracts + spread_contracts must equal total_contracts")

    quantities: Counter[str] = Counter()
    single_put_contracts = spread_contracts = gross_credit = cash_collateral = defined_risk = 0

    for item in board:
        ticker = str(item.get("ticker") or "").upper()
        strategy = str(item.get("strategy") or "").upper()
        qty = int(item.get("qty") or 0)
        dte = int(item.get("dte") or -1)
        if not ticker or strategy not in ALLOWED_STRATEGIES or qty <= 0:
            raise ValueError(f"invalid ticker/strategy/qty in board row: {item}")
        if dte < 0 or (strategy in SINGLE_PUT_STRATEGIES and dte > csp_cap) or (strategy in SPREAD_STRATEGIES and dte >= spread_cap):
            raise ValueError(f"DTE policy breach for {ticker} {strategy}: {dte}")
        if "short_bid" not in item or n(item["short_bid"]) <= 0:
            raise ValueError(f"missing positive short_bid for {ticker}")
        if strategy in SPREAD_STRATEGIES:
            if "long_ask" not in item or n(item["long_ask"]) <= 0:
                raise ValueError(f"missing positive long_ask for {ticker} {strategy}")
            if n(item.get("max_loss_per_contract")) <= 0:
                raise ValueError(f"missing positive max_loss_per_contract for {ticker} {strategy}")
            spread_contracts += qty
        else:
            if n(item.get("collateral_per_contract")) <= 0:
                raise ValueError(f"missing positive collateral_per_contract for {ticker} {strategy}")
            single_put_contracts += qty
        if n(item.get("reference_credit_per_contract")) <= 0:
            raise ValueError(f"missing positive reference_credit_per_contract for {ticker}")

        quantities[ticker] += qty
        gross_credit += qty * round(n(item["reference_credit_per_contract"]))
        cash_collateral += qty * round(n(item.get("collateral_per_contract", 0)))
        defined_risk += qty * round(n(item.get("max_loss_per_contract", 0)))

    if sum(quantities.values()) != total:
        raise ValueError(f"board has {sum(quantities.values())} contracts; expected {total}")
    if single_put_contracts != single_target or spread_contracts != spread_target:
        raise ValueError(
            f"allocation breach: single-put={single_put_contracts}/{single_target}, "
            f"spread={spread_contracts}/{spread_target}"
        )
    breached = {ticker: qty for ticker, qty in quantities.items() if qty > max_per_ticker}
    if breached:
        raise ValueError(f"ticker concentration exceeds {max_per_ticker}: {breached}")

    four_week_plan = manifest.get("four_week_plan")
    if not isinstance(four_week_plan, list) or len(four_week_plan) != 4:
        raise ValueError("manifest.four_week_plan must contain exactly four re-screen weeks")
    plan_fields = {"week", "date_range", "catalysts", "book_action", "re_strike_queue", "conditions_and_vetoes", "strategies", "credit_plan"}
    for index, plan in enumerate(four_week_plan, start=1):
        if not isinstance(plan, dict):
            raise ValueError(f"four_week_plan row {index} must be an object")
        missing = plan_fields - set(plan)
        if missing:
            raise ValueError(f"four_week_plan row {index} missing required operating-plan fields: {sorted(missing)}")

    portfolio = manifest.get("portfolio_context")
    portfolio_fields = {"snapshot_as_of", "summary", "triage_rule", "credit_separation"}
    if not isinstance(portfolio, dict):
        raise ValueError("manifest.portfolio_context must be an object")
    missing = portfolio_fields - set(portfolio)
    if missing:
        raise ValueError(f"manifest.portfolio_context missing required fields: {sorted(missing)}")

    return board, {
        "gross_credit": gross_credit,
        "cash_collateral": cash_collateral,
        "defined_risk": defined_risk,
        "contracts": sum(quantities.values()),
        "tickers": len(quantities),
        "single_put_contracts": single_put_contracts,
        "spread_contracts": spread_contracts,
    }


def render(manifest: dict[str, Any]) -> str:
    board, totals = validate(manifest)
    rules = manifest["board_rule"]
    as_of = manifest.get("as_of", "unspecified timestamp")
    quote_basis = manifest.get("quote_basis", "historical quote snapshot")
    confidence = {"High": "High", "Medium": "Medium", "Low-Medium": "Low–Med", "Low": "Low"}
    table_rows: list[str] = []

    for item in board:
        strategy = item["strategy"]
        if strategy in SINGLE_PUT_STRATEGIES:
            quote = f"bid/ask {n(item['short_bid']):.2f}/{n(item.get('short_ask')):.2f}; {money(n(item['reference_credit_per_contract']))}"
            delta = item.get("delta", item.get("short_delta"))
            risk = f"cash {money(n(item['collateral_per_contract']))}; Δ {n(delta):+.3f}"
            liquidity = f"OI {maybe_count(item.get('open_interest', item.get('short_oi')))}; vol {maybe_count(item.get('volume', item.get('short_volume')))}"
        else:
            raw_credit = round((n(item["short_bid"]) - n(item["long_ask"])) * 100)
            reference_credit = round(n(item["reference_credit_per_contract"]))
            quote = f"short bid {n(item['short_bid']):.2f} − long ask {n(item['long_ask']):.2f} = {money(raw_credit)}"
            if raw_credit != reference_credit:
                quote += f"; policy planning credit {money(reference_credit)}"
            risk = f"max loss {money(n(item['max_loss_per_contract']))}; RoR {n(item['return_on_risk']):.1%}; Δ {n(item['short_delta']):+.3f}"
            liquidity = f"short OI {maybe_count(item.get('short_oi'))}; vol {maybe_count(item.get('short_volume'))}"
        evidence = f"{item['source']}; flow {item['flow']}; {item['stldc']}; DP {item['dark_pool']}"
        table_rows.append(row([
            item["rank"], item["ticker"], f"{strategy} ×{item['qty']}", item["contract"],
            f"{item['expiry']} / {item['dte']}d", quote, risk, liquidity, item["earnings"],
            confidence.get(item.get("confidence"), item.get("confidence", "—")), evidence, item["gate"],
        ]))

    budget = manifest.get("budget", {})
    weekly_target = budget.get("weekly_reference_range", [5000, 6000])
    monthly_target = budget.get("monthly_reference_range", [20000, 30000])
    portfolio = manifest["portfolio_context"]
    roadmap_rows = []
    for plan in manifest["four_week_plan"]:
        roadmap_rows.append(row([
            plan["week"], plan["date_range"], plan["catalysts"], plan["book_action"],
            plan["re_strike_queue"], plan["conditions_and_vetoes"], plan["strategies"], plan["credit_plan"],
        ]))

    return f'''## Part 4 · 期权收入板 | Weekly Top-20 Options Income Board & Four-Week Re-screen Plan
<callout icon="⚠️" color="yellow_bg"><b>Research review only — not orders.</b> This compact 20-contract menu is based on <b>{esc(as_of)}</b>. {esc(quote_basis)} The board is a historical, evidence-backed planning snapshot, not a live quotation, expected return, or execution instruction. Every selected sleeve requires a fresh opening NBBO, spot, OI, DTE, earnings/event, flow/reconciliation, account-risk, assignment-capacity, and suitability check. Covered calls are excluded because share ownership was not verified.</callout>

### 4.0 任务、风险边界与资金框架 | Mandate, controls & capital frame
- <b>Selection:</b> only the selected highest-ranked, evidenced sleeves are shown; raw full-source ledgers are retained only in the accompanying JSON source snapshot.
- <b>Allocation:</b> {totals['contracts']} contracts across {totals['tickers']} tickers; maximum {rules['max_contracts_per_ticker']} contracts per ticker; <b>single-put family (SP/CSP/WHEEL_CSP) {totals['single_put_contracts']}</b> versus <b>defined-risk BPS/BCS {totals['spread_contracts']}</b> = 50:50. No naked calls, strangles, or inferred covered calls.
- <b>DTE:</b> SP/CSP/WHEEL_CSP ≤{rules['csp_dte_cap']} days; BPS/BCS &lt;{rules['spread_dte_cap']} days. Historical quote timestamp: {esc(as_of)}.
- <b>Reference gross credit:</b> {money(totals['gross_credit'])} for this historical board, within the {money(weekly_target[0])}–{money(weekly_target[1])} weekly planning band. Four separately rebuilt weeks at that band imply {money(weekly_target[0] * 4)}–{money(weekly_target[1] * 4)} gross credit; the broader {money(monthly_target[0])}–{money(monthly_target[1])} monthly ambition is not pre-budgeted and is <b>not expected profit</b>.
- <b>Capital reference:</b> cash-secured collateral {money(totals['cash_collateral'])}; defined-risk maximum loss {money(totals['defined_risk'])}. Validate NLV, existing exposure, margin treatment, concentration, and assignment capacity before any independent action.
- <b>Opening rule:</b> use a new NBBO; skip an entire sleeve if credit is below 80% of the reference, liquidity/event/reconciliation changes, or the effective regime is downgraded. Do not replace a failed sleeve by increasing another ticker.

### 4.0A 持仓语境与优先分诊 | Portfolio context & triage priority
<callout icon="🧭" color="gray_bg"><b>Historical control context, not a current broker statement.</b> {esc(portfolio['snapshot_as_of'])}. {esc(portfolio['summary'])}</callout>
- <b>First action:</b> {esc(portfolio['triage_rule'])}
- <b>Income separation:</b> {esc(portfolio['credit_separation'])}

### 4.1 本周最高置信度板 | This week’s highest-confidence 20-contract board
<table fit-page-width="true" header-row="true">
<tr><th>#</th><th>Ticker</th><th>Structure / qty</th><th>Contract</th><th>Expiry / DTE</th><th>Historical NBBO reference</th><th>Risk / delta</th><th>Liquidity</th><th>Earnings</th><th>Confidence</th><th>Selected-source evidence</th><th>Opening gate</th></tr>
{''.join(table_rows)}
</table>

### 4.2 未来四周收入作战计划 | Four-week income operating plan
<table fit-page-width="true" header-row="true">
<tr><th>Week / dates</th><th>Catalysts</th><th>Book / income action</th><th>Post-print re-strike queue — not pre-approved</th><th>Only-if conditions &amp; vetoes</th><th>Permitted structure</th><th>Gross-credit treatment</th></tr>
{''.join(roadmap_rows)}
</table>
<b>Operating rule:</b> a queue is not a ticket. A name becomes eligible only after the listed post-print and opening checks pass; a veto removes it even when premium is attractive. Do not substitute a failed queue item by increasing another ticker.

<b>Monthly planning math:</b> four independently rebuilt {money(weekly_target[0])}–{money(weekly_target[1])} qualified boards imply {money(weekly_target[0] * 4)}–{money(weekly_target[1] * 4)} gross credit. Any roll, covered-call, diagonal, or extra-credit contribution remains excluded until it is independently validated; gross credit is not profit and excludes losses, buy-backs, assignment, exercise, fees, slippage, margin changes, and roll costs.

### 4.3 处理与排除 | Handling & exclusions
- Historical credits are source marks, not P&L or current premiums. Do not average down; re-evaluate threatened short strikes only with current chain and current portfolio exposure.
- Covered-call rows, candidate rows with unverified share ownership, negative/invalid risk fields, stale event data, and candidates failing a fresh opening gate are excluded rather than used to fill the 20-contract target.
- A fresh lower regime, a Part 2 reconciliation conflict, earnings inside the selected expiry, an adverse structural break, or a material loss of liquidity requires withholding/rebuilding the affected sleeve.

---
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and render a review-only weekly top-20 income board")
    parser.add_argument("--manifest", required=True, help="Input JSON manifest")
    parser.add_argument("--out", required=True, help="Output Part 4 Markdown path")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rendered = render(manifest)
    Path(args.out).write_text(rendered, encoding="utf-8")
    _, totals = validate(manifest)
    print(
        f"Board: {totals['contracts']} contracts / {totals['single_put_contracts']} single-put / "
        f"{totals['spread_contracts']} spreads / {totals['tickers']} tickers / "
        f"gross reference {money(totals['gross_credit'])}"
    )
    print(f"Output: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
