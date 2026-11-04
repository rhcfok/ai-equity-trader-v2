#!/usr/bin/env python3
"""Validate and render a research-only weekly board. No market/data writes/orders.
Current policy: weak regime is not a blanket put veto; soft6put/4CC/10spread,
20-unit target; exact existing em-probability ranking and verified CC coverage.
Legacy modes are read-only compatibility for old review manifests, not defaults.
"""
from __future__ import annotations
import argparse
import html
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from map_em_probabilities import CALLS, PUTS, SPREADS, US_EXCHANGES, check_score, family

SINGLE_PUT_STRATEGIES=PUTS
COVERED_CALL_STRATEGIES=CALLS
SPREAD_STRATEGIES=SPREADS
ALLOWED_STRATEGIES=PUTS|CALLS|SPREADS
POOR_REGIMES={'CAUTIOUS','DEFENSIVE'}


def esc(value:Any)->str:return html.escape(str(value),quote=False)
def row(values:list[Any])->str:
    return '  <tr>\n'+'\n'.join('    <td>'+esc(x)+'</td>' for x in values)+'\n  </tr>'
def table(headers,rows):return '<table fit-page-width="true" header-row="true">\n'+row(headers)+'\n'+'\n'.join(rows)+'\n</table>'
def money(value)->str:return f'${float(value):,.0f}'
def n(value)->float:return float(value)
def maybe_count(value)->str:
    try:return f'{int(float(value)):,}'
    except (TypeError,ValueError):return 'n/a'


def validate(manifest:dict[str,Any])->tuple[list[dict],dict]:
    rules=manifest.get('board_rule',{});board=manifest.get('board')
    if not isinstance(board,list) or not board:raise ValueError('manifest.board must be a non-empty list')
    total=int(rules.get('total_contracts',20));max_ticker=int(rules.get('max_contracts_per_ticker',2))
    csp_cap=int(rules.get('csp_dte_cap',35));spread_cap=int(rules.get('spread_dte_cap',45));cc_cap=int(rules.get('cc_dte_cap',20))
    mode=rules.get('selection_mode','ranked_30_20_50');regime=str(manifest.get('effective_regime') or '').upper()
    if mode not in {'ranked_30_20_50','full_50_50','regime_spread_only'}:raise ValueError('unknown selection_mode')
    if total!=20 or not 1<=max_ticker<=2 or csp_cap>35 or spread_cap>45 or cc_cap>20:raise ValueError('board cannot exceed20 units,2/ticker,CSP35d,CC20d,spread<45d')
    if mode=='full_50_50' and (int(rules.get('single_put_contracts',10))!=10 or int(rules.get('spread_contracts',10))!=10):raise ValueError('legacy full_50_50 targets10/10')
    if mode=='regime_spread_only' and regime not in POOR_REGIMES:raise ValueError('legacy regime_spread_only requires CAUTIOUS/DEFENSIVE')
    if mode=='ranked_30_20_50' and (int(rules.get('single_put_contracts',6)),int(rules.get('covered_call_contracts',4)),int(rules.get('spread_contracts',10)))!=(6,4,10):raise ValueError('soft target must be6 single puts,4CC,10 spreads')
    quantities=Counter();call_quantities=Counter();share_capacity={};mix=Counter();gross=cash=risk=stock_capital=0
    previous_probability=float('inf')
    for item in board:
        ticker=str(item.get('ticker') or '').upper();strategy=str(item.get('strategy') or '').upper();qty=int(item.get('qty') or 0)
        dte=int(item['dte']) if item.get('dte') is not None else -1
        if not ticker or strategy not in ALLOWED_STRATEGIES or qty<1:raise ValueError('invalid ticker/strategy/quantity')
        # No regime-only CSP/SP veto. Regime affects risk budget and discipline.
        fam=family(strategy)
        if dte<0 or (fam=='single_put' and dte>csp_cap) or (fam=='spread' and dte>=spread_cap) or (fam=='covered_call' and dte>cc_cap):raise ValueError(f'DTE breach:{ticker}/{strategy}/{dte}')
        if n(item.get('short_bid',0))<=0:raise ValueError('positive short_bid required')
        if fam=='spread':
            if n(item.get('long_ask',0))<=0 or n(item.get('max_loss_per_contract',0))<=0:raise ValueError('spread positive long_ask/max_loss required')
        elif fam=='single_put':
            if n(item.get('collateral_per_contract',0))<=0:raise ValueError('put collateral/assignment reserve required')
        else:
            if item.get('share_ownership_verified') is not True:raise ValueError('CC share ownership unverified; never naked call')
            if n(item.get('shares_available',0))<qty*100:raise ValueError('CC requires100 unencumbered shares per unit')
            date.fromisoformat(str(item.get('share_snapshot_as_of') or '')[:10])
            if n(item.get('stock_capital_per_contract',0))<=0:raise ValueError('CC stock capital/downside context required')
            call_quantities[ticker]+=qty
            share_capacity[ticker]=min(share_capacity.get(ticker,float('inf')),int(n(item['shares_available'])))
        if n(item.get('reference_credit_per_contract',0))<=0:raise ValueError('positive reference credit required')
        if mode=='ranked_30_20_50':
            if str(item.get('exchange') or '').upper() not in US_EXCHANGES:raise ValueError('US exchange missing')
            em=item.get('em_probability')
            if not isinstance(em,dict) or em.get('match_status')!='matched' or not em.get('identity_checked'):raise ValueError('exact matched EM measurement required; no ticker-only or synthetic win rate')
            vintage=str(manifest.get('model_as_of') or '')
            if not vintage:raise ValueError('model_as_of completed session required')
            check_score(item,em,vintage)
            p=n(em['p_win_expiry'])
            if p>previous_probability:raise ValueError('board must be descending p_win_expiry')
            previous_probability=p
        quantities[ticker]+=qty;mix[fam]+=qty
        gross+=qty*round(n(item['reference_credit_per_contract']))
        cash+=qty*round(n(item.get('collateral_per_contract',0)))
        risk+=qty*round(n(item.get('max_loss_per_contract',0)))
        stock_capital+=qty*round(n(item.get('stock_capital_per_contract',0)))
    count=sum(quantities.values())
    if count>20:raise ValueError('more than20 strategy units')
    if any(x>max_ticker for x in quantities.values()):raise ValueError('ticker concentration breach')
    if any(qty*100>share_capacity[ticker] for ticker,qty in call_quantities.items()):raise ValueError('CC shares double-counted across tickets')
    if mode=='full_50_50' and (count!=20 or mix['single_put']!=10 or mix['spread']!=10 or mix['covered_call']):raise ValueError('legacy full allocation must be20 at10/10')
    if mode=='regime_spread_only' and (mix['single_put'] or mix['covered_call']):raise ValueError('legacy spread-only board accepts only BPS/BCS')
    # Current ratio is approximate. Show deviations, do not falsify rejected/missing supply.
    plan=manifest.get('four_week_plan')
    fields={'week','date_range','catalysts','book_action','re_strike_queue','conditions_and_vetoes','strategies','credit_plan'}
    if not isinstance(plan,list) or len(plan)!=4:raise ValueError('exactly4 operating weeks required')
    if any(not isinstance(w,dict) or fields-set(w) for w in plan):raise ValueError('missing four-week operating details')
    portfolio=manifest.get('portfolio_context');fields={'snapshot_as_of','summary','triage_rule','credit_separation'}
    if not isinstance(portfolio,dict) or fields-set(portfolio):raise ValueError('missing portfolio context')
    return board,{'gross_credit':gross,'cash_collateral':cash,'defined_risk':risk,'stock_capital':stock_capital,'contracts':count,'tickers':len(quantities),'single_put_contracts':mix['single_put'],'covered_call_contracts':mix['covered_call'],'spread_contracts':mix['spread']}


def render(manifest:dict[str,Any])->str:
    board,totals=validate(manifest);rules=manifest['board_rule'];mode=rules.get('selection_mode','ranked_30_20_50')
    as_of=manifest.get('as_of','missing');quote_basis=manifest.get('quote_basis','dated research snapshot; not live')
    if mode=='ranked_30_20_50':
        allocation='Soft target6 CSP/SP/WHEEL_CSP +4 verified CC +10 BPS/BCS (30/20/50). Actual mix is shown; report any verified-supply deviation. Weak regime alone does not block single puts.'
    elif mode=='regime_spread_only':allocation='Legacy spread-only selected menu, not the current default; fewer than20 may reflect supply/validation. No regime-only ban on CSP/SP in the current policy.'
    else:allocation='Legacy50:50 archived manifest; current new runs use soft30/20/50.'
    rows=[]
    for item in board:
        fam=family(item['strategy']);em=item.get('em_probability') or {}
        quote=f"short bid {n(item['short_bid']):.2f}"
        if fam=='spread':
            quote+=f" minus long ask {n(item['long_ask']):.2f}; reference {money(item['reference_credit_per_contract'])}"
            risk=f"Max payoff loss {money(item['max_loss_per_contract'])}; RoR {n(item['return_on_risk']):.1%}"
        elif fam=='covered_call':
            quote+=f"; reference {money(item['reference_credit_per_contract'])}"
            risk=f"Verified shares{item['shares_available']}; stock capital {money(item['stock_capital_per_contract'])}/unit; underlying downside remains"
        else:
            quote+=f"; reference {money(item['reference_credit_per_contract'])}"
            risk=f"Cash/assignment reserve {money(item['collateral_per_contract'])}"
        prob=f"{n(em['p_win_expiry']):.1%}" if em.get('p_win_expiry') is not None else 'Unmapped legacy fixture; not ranked under current policy'
        p50=f"{n(em['p_profit50_first']):.1%}" if em.get('p_profit50_first') is not None else 'N/A'
        identity=f"{item.get('source_table','legacy')}; {item.get('opportunity_key','missing')}; score proposal date{em.get('report_date','n/a')}; sigma{em.get('sigma_as_of','n/a')}; {em.get('model_version','n/a')}"
        identity+=f"; calibration{em.get('calib_cov_30d','n/a')}; outside1.12band{em.get('outside_band112','n/a')}; earnings-window{em.get('earnings_in_window','n/a')}"
        if em.get('calibration_note'):identity+='; '+em['calibration_note']
        evidence=f"{item.get('source','')}; flow{item.get('flow','')}; STLDC{item.get('stldc','')}; DP{item.get('dark_pool','')}"
        rows.append(row([item['rank'],item['ticker'],f"{item['strategy']} ×{item['qty']}",item['contract'],f"{item['expiry']} /{item['dte']}d",prob,p50,identity,quote,risk,item.get('earnings','unknown'),item.get('confidence','not a model probability'),evidence,item.get('gate','fresh opening checks required')]))
    plan_rows=[row([w['week'],w['date_range'],w['catalysts'],w['book_action'],w['re_strike_queue'],w['conditions_and_vetoes'],w['strategies'],w['credit_plan']]) for w in manifest['four_week_plan']]
    budget=manifest.get('budget',{});weekly=budget.get('weekly_reference_range',[5000,6000]);portfolio=manifest['portfolio_context']
    gross=totals['gross_credit'];shortfall=max(0,weekly[0]-gross)
    return f'''## Part 4 · 期权收入板 | Probability-ranked Top-20 income plan

<callout color="yellow_bg">Research only, no orders. {esc(as_of)}; {esc(quote_basis)}. A probability-ranked proposal is not a live quote, an approved allocation, a guaranteed income stream or a realized win rate. Independently refresh quotes, events, cash/assignment capacity, CC shares and aggregate risk before entry.</callout>

### 4.0 风险、配比和资金 | Risk, allocation & capital
- Selection: only selected exact-source tickets; raw source ledgers stay in the source snapshot.
- Allocation: {totals['contracts']} strategy units/{totals['tickers']} tickers, maximum{rules.get('max_contracts_per_ticker',2)}/ticker; single-put {totals['single_put_contracts']}, covered-call {totals['covered_call_contracts']}, spread {totals['spread_contracts']}. {esc(allocation)} One spread unit includes two raw legs; no naked calls.
- DTE: SP/CSP/WHEEL_CSP≤35d; BPS/BCS&lt;45d. Existing CC short-call≤20d discipline retained unless separately changed.
- Regime: affects size, collateral, credit-risk budget, entry timing and support/valuation margin; not a blanket CSP/SP veto. Low probability has no invented numeric cutoff; independent technical/event/risk vetoes remain.
- Gross reference credit: {money(gross)} versus {money(weekly[0])}–{money(weekly[1])} weekly planning, lower-edge gap {money(shortfall)}. Do not expand risk to fill missing credit. Four separately rebuilt qualifying weeks imply {money(weekly[0]*4)}–{money(weekly[1]*4)} gross credit, not expected profit.
- Capital reference: cash/assignment reserve{money(totals['cash_collateral'])}; spread payoff maximum loss{money(totals['defined_risk'])}; CC stock capital{money(totals['stock_capital'])}. Cash reserve and CC stock capital are NOT capped maximum loss. Account/NLV unknown means no account-approved size.

### 4.0A 持仓分诊 | Portfolio triage
{esc(portfolio['snapshot_as_of'])}: {esc(portfolio['summary'])}

First action: {esc(portfolio['triage_rule'])}. Credit separation: {esc(portfolio['credit_separation'])}. New CC overwrite proposals may be included only against verified unencumbered shares, with the share/book snapshot labeled; do not recount already-open call premium.

### 4.1 最高模型胜率提案 | Highest modeled win-probability proposals
{table(['Rank','Ticker','Structure/qty','Contract legs','Expiry/DTE','EM win probability: short leg OTM','50% profit-before-touch','Exact score mapping/source vintage','Dated credit reference','Risk/capital','Earnings','Qualitative confidence','Evidence','Opening status/gate'],rows)}

Win rate here means stored em-probability.p_win_expiry = P(short leg finishes OTM), not empirical realized profit. It is not the full covered-call stock+option return probability or a spread breakeven probability. p_profit50_first is the separate model approximation for taking half the credit before strike touch; it is sensitive to source credit adapters. No 1−delta estimate, ticker-only score transfer, old strike or event-invalid score replaces an exact match. Model math/version unchanged; ranking is a user-selected use of the measurement, not promotion to autonomous probability gating.

### 4.2 未来四周操作计划 | Four-week operating plan
{table(['Week','Dates','Catalysts','Book/income action','Post-print re-strike queue','Only-if/veto','Structures/mix','Credit counted'],plan_rows)}

A queue is not a ticket. Refresh each week independently. CC stock return, prior position premium, unverified rolls, diagonals and unresolved tickets are excluded from new-opening gross-credit totals. Gross premium is not P&L; deduct buybacks, losses, fees, slippage and stock/assignment exposure.

### 4.3 排除与偏差 | Exclusions & deviations
{esc(json.dumps(manifest.get('allocation_deviations',[]),ensure_ascii=False))}

Missing/stale/mismatched EM records stay unranked, never assigned a made-up win rate. Missing CC coverage keeps the affected candidate on hold, not converted to a naked call. Individual quote/event/flow/support/risk failures withhold the affected ticket; a weak regime alone does not cancel otherwise eligible CSP/SP/CC/BPS/BCS.
'''


def main():
    parser=argparse.ArgumentParser(description='Validate/render a research-only probability-ranked30/20/50 board')
    parser.add_argument('--manifest',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args();manifest=json.loads(Path(args.manifest).read_text())
    Path(args.out).write_text(render(manifest),encoding='utf-8')
    _,t=validate(manifest);print(f"Board:{t['contracts']} units; put{t['single_put_contracts']}/CC{t['covered_call_contracts']}/spread{t['spread_contracts']}; gross reference{money(t['gross_credit'])}")

if __name__=='__main__':main()
