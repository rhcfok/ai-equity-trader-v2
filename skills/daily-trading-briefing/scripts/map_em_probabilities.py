#!/usr/bin/env python3
"""Map existing em-probability scores, then rank research candidates. No model math,
network, database writes, or order capabilities. Probabilities are measurements,
not proof of future profit or an automated probability-threshold veto.
"""
from __future__ import annotations
import argparse
import json
import math
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

SOURCE_TABLES={'option-opportunity-davis','csp-bps-opportunity','uw-option-opportunities'}
PUTS={'SP','CSP','WHEEL_CSP'}
CALLS={'CC','WHEEL_CC','COVERED_CALL'}
SPREADS={'BPS','BCS','BULL_PUT_SPREAD','BEAR_CALL_SPREAD'}
US_EXCHANGES={'NASDAQ','NYSE','AMEX','NYSEARCA','NYSEAMERICAN','CBOE','BATS','ARCA','NYSEMKT','NYSE ARCA','NYSE AMERICAN'}
METRIC_LABEL='P(short leg OTM at expiry)'
TARGETS={'single_put':6,'covered_call':4,'spread':10}


def family(strategy: str) -> str:
    s=str(strategy).upper()
    if s in PUTS:return 'single_put'
    if s in CALLS:return 'covered_call'
    if s in SPREADS:return 'spread'
    raise ValueError('Unsupported income strategy: '+s)


def canonical_strategy(strategy: str) -> str:
    return {'BULL_PUT_SPREAD':'BPS','BEAR_CALL_SPREAD':'BCS','COVERED_CALL':'CC','WHEEL_CC':'CC'}.get(str(strategy).upper(),str(strategy).upper())


def number(value: Any) -> float:
    x=float(value)
    if not math.isfinite(x):raise ValueError('Nonfinite numeric input')
    return x


def check_score(candidate: dict,score: dict,model_as_of: str) -> None:
    if candidate.get('source_table') not in SOURCE_TABLES:raise ValueError('unknown source table')
    if (candidate.get('source_table'),candidate.get('opportunity_key'))!=(score.get('source_table'),score.get('opportunity_key')):raise ValueError('source/key mismatch')
    symbol=str(candidate.get('ticker') or candidate.get('symbol') or '').upper()
    if not symbol or symbol!=str(score.get('symbol') or '').upper():raise ValueError('symbol mismatch')
    if str(candidate.get('strategy')).upper()!=str(score.get('strategy')).upper():raise ValueError('strategy mismatch')
    if str(candidate.get('expiry'))[:10]!=str(score.get('expiry'))[:10]:raise ValueError('expiry mismatch')
    if number(candidate.get('short_strike'))!=number(score.get('strike')):raise ValueError('short strike mismatch')
    source_date=candidate.get('source_report_date',candidate.get('report_date'))
    if not source_date or str(source_date)[:10]!=str(score.get('report_date'))[:10]:raise ValueError('source report date mismatch')
    if str(score.get('sigma_as_of'))[:10]!=model_as_of:raise ValueError('model vintage mismatch (missing, lagged or future sigma)')
    date.fromisoformat(model_as_of)
    if candidate.get('decision_date') and date.fromisoformat(str(score['report_date'])[:10])>date.fromisoformat(candidate['decision_date']):raise ValueError('future proposal date')
    if not str(score.get('model_version') or '').strip():raise ValueError('model_version missing')
    p=number(score.get('p_win_expiry'))
    if not 0<=p<=1:raise ValueError('p_win_expiry must be fraction0..1')
    if 'p_profit50_first' not in score:raise ValueError('p_profit50_first field missing; explicit null allowed')
    p50=score.get('p_profit50_first')
    if p50 is not None and not 0<=number(p50)<=1:raise ValueError('p_profit50_first must be fraction0..1')
    calib=score.get('calib_cov_30d')
    if calib is not None and not 0<=number(calib)<=1:raise ValueError('calibration coverage must be fraction0..1 or null')
    if score.get('earnings_in_window') is True:raise ValueError('earnings-blind model invalid for holding-window event')
    if score.get('earnings_in_window') is not False:raise ValueError('earnings flag missing')


def attach_probabilities(candidates: list[dict],scores: list[dict],*,model_as_of: str) -> dict:
    """Never match by ticker alone, recompute a probability, or fill missing values."""
    index={}
    duplicates=set()
    for score in scores:
        key=(score.get('source_table'),score.get('opportunity_key'))
        if key in index:duplicates.add(key)
        index[key]=score
    ranked=[];excluded=[]
    for original in candidates:
        item=deepcopy(original)
        key=(item.get('source_table'),item.get('opportunity_key'))
        try:
            if key in duplicates:raise ValueError('duplicate exact EM key')
            if key not in index:raise ValueError('no exact em-probability record')
            score=index[key]
            check_score(item,score,model_as_of)
            item['em_probability']=deepcopy(score)
            item['em_probability'].update(match_status='matched',metric_label=METRIC_LABEL,identity_checked=True)
            if score.get('calib_cov_30d') is None:
                item['em_probability']['calibration_note']='Calibration coverage unavailable; extra skepticism, not an invented probability cutoff.'
            ranked.append(item)
        except (ValueError,TypeError,KeyError) as exc:
            excluded.append({'ticker':item.get('ticker',item.get('symbol')),'source_table':key[0],'opportunity_key':key[1],'reason':str(exc),'status':'UNRANKED_MODEL_UNAVAILABLE_OR_INVALID'})
    return {'candidates':ranked,'exclusions':excluded,'metric':METRIC_LABEL,'model_as_of':model_as_of}


def select_top20(candidates: list[dict]) -> dict:
    """Soft6/4/10 mix, descending EM measurement within eligible supply; max2/ticker.

    A spread counts as one strategy unit, not two raw legs. Approximate quotas may
    deviate when verified supply is insufficient; report actual mix. A missing
    score or unowned CC is never replaced with an invented proposal or naked call.
    """
    pool=[];deviations=[]
    for original in candidates:
        item=deepcopy(original)
        try:
            fam=family(item['strategy']);em=item['em_probability']
            if em.get('match_status')!='matched' or not em.get('identity_checked'):raise ValueError('EM identity not verified')
            if str(item.get('exchange') or '').upper() not in US_EXCHANGES:raise ValueError('US exchange not verified')
            if item.get('eligible_for_ranking') is not True:raise ValueError('independent quote/event/risk checks not accepted for ranking')
            if fam=='covered_call' and (item.get('share_ownership_verified') is not True or number(item.get('shares_available',0))<100):raise ValueError('CC share coverage unavailable')
            if fam=='covered_call':
                snapshot=date.fromisoformat(str(item.get('share_snapshot_as_of') or '')[:10])
                if item.get('decision_date') and snapshot>date.fromisoformat(item['decision_date']):raise ValueError('future CC holdings snapshot')
            item['ticker']=str(item.get('ticker') or item.get('symbol')).upper()
            # qty is available research allocation, never automatically doubled to fill slots.
            available=int(item.get('qty',1))
            if available<1:raise ValueError('invalid quantity')
            item['qty']=min(available,2)
            if fam=='covered_call':item['qty']=min(item['qty'],int(number(item['shares_available'])//100))
            pool.append(item)
        except (KeyError,ValueError,TypeError) as exc:
            deviations.append({'ticker':item.get('ticker',item.get('symbol')),'reason':str(exc),'type':'candidate_not_eligible'})
    def key(item):
        score=item['em_probability'];p50=score.get('p_profit50_first')
        return (-number(score['p_win_expiry']),-number(p50) if p50 is not None else 1,item['ticker'],item['source_table'],item['opportunity_key'])
    pool.sort(key=key)
    ticker_qty=Counter();ticket_qty=Counter();mix=Counter();chosen={}
    def take(item,cap):
        identity=(item['source_table'],item['opportunity_key']);sym=item['ticker'];fam=family(item['strategy'])
        own_limit=2
        if fam=='covered_call':own_limit=min(2,int(number(item['shares_available'])//100))
        count=min(cap,item['qty']-ticket_qty[identity],own_limit-ticker_qty[sym],20-sum(mix.values()))
        if count<=0:return
        ticker_qty[sym]+=count;ticket_qty[identity]+=count;mix[fam]+=count
        if identity not in chosen:chosen[identity]={**item,'qty':0}
        chosen[identity]['qty']+=count
    for fam,target in TARGETS.items():
        for item in pool:
            if family(item['strategy'])==fam:take(item,target-mix[fam])
    # Soft ratio: fill remaining verified supply at highest probability, not synthetic quantity.
    for item in pool:take(item,20-sum(mix.values()))
    board=sorted(chosen.values(),key=key)
    for rank,item in enumerate(board,1):item['rank']=rank
    allocation={fam:mix[fam] for fam in TARGETS}
    for fam,target in TARGETS.items():
        if mix[fam]!=target:deviations.append({'family':fam,'target':target,'actual':mix[fam],'type':'soft_mix_deviation'})
    if sum(mix.values())<20:deviations.append({'type':'shortfall','target':20,'actual':sum(mix.values()),'reason':'insufficient independently verified, score-matched supply under concentration/share limits'})
    return {'board':board,'allocation':allocation,'deviations':deviations,'total_units':sum(mix.values()),'ranking_metric':METRIC_LABEL}


def main():
    parser=argparse.ArgumentParser(description='Read-only exact EM mapping and ranked30/20/50 Top20 selection')
    parser.add_argument('--candidates',required=True)
    parser.add_argument('--scores',required=True)
    parser.add_argument('--model-as-of',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    mapped=attach_probabilities(json.loads(Path(args.candidates).read_text()),json.loads(Path(args.scores).read_text()),model_as_of=args.model_as_of)
    result=select_top20(mapped['candidates']);result['probability_mapping']=mapped
    Path(args.out).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(f"Selected {result['total_units']}/20 research units; mix{result['allocation']}; missing/invalid EM {len(mapped['exclusions'])}")

if __name__=='__main__':main()
