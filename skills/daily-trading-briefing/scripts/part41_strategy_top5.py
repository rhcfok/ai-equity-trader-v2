#!/usr/bin/env python3
"""Part 4.1 v2: top-5 per strategy family (SP/SC/BPS/BCS) by evidence-weighted confidence.

Weights calibrated from win-rate grading (session sess_bacf128e, window 2026-10-08 -> 11-13,
falling market): jlaw-yahoo Watch 61.6% 5d / 67.4% 10d = strongest long-side evidence (+2.0);
flow-verdict bullish 40.0% 5d is BELOW base (long side only +0.5/+0.25); flow-verdict bearish
64.8% 5d / 75% 10d = strongest short-side evidence (+3.0/+1.5); STLDC chained BULL-CONFIRM
56.9% (fresh 57.1% vs stale 53.5%) -> fresh CONFIRM aligned +1.5 / stale +0.75, ALERT aligned 0;
dark pool & unusual flow not independently graded -> corroboration +/-1.0; quant2
probability_status=not_validated -> +1.0 (rank<=20) / +0.5 (<=100). Opposed evidence subtracts
symmetrically. flow status=conflict hard -2.0.

Read-only: reads local snapshots, writes one markdown file. No network, no DB writes, no orders.

Input snapshots (under --indir, produced by the daily export step):
  em_scores.json            em-probability rows for the scored batch (report_date = --batch-date)
  ledger_davis.json         option-opportunity-davis rows (same batch)
  ledger_uw.json            uw-option-opportunities rows (same batch)
  flow_reconcile_<vdate>.json
  stldc_latest.json         trade-stldc-v2 latest run
  quant2_leaderboard.json   one run of quant2_leaderboard
  darkpool_recent.json      darkpool_symbol_daily rows (filter fetch_date = <dp-date>)
  normalized_flow_recent.json  option_flow_symbol_signals rows (filter signal_date = <nf-date>)
  valuation_full.json       equity_valuation_uw_daily full-column rows (reference columns only)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FAMILY = {"SHORT_PUT": "SP", "CSP": "SP", "WHEEL_CSP": "SP",
          "WHEEL_CC": "SC", "CC": "SC",
          "BULL_PUT_SPREAD": "BPS", "BEAR_CALL_SPREAD": "BCS"}
SIDE = {"SP": 1, "BPS": 1, "SC": -1, "BCS": -1}
FAM_TITLES = [("SP", "卖 Put | Short Put"), ("SC", "卖 Call | Short Call"),
              ("BPS", "牛市看跌价差 | Bull Put Spread"), ("BCS", "熊市看涨价差 | Bear Call Spread")]
FAM_IDX = {"SP": 1, "SC": 2, "BPS": 3, "BCS": 4}


def n(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_creds_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Part 4.1 per-strategy top-5 confidence markdown (read-only)")
    ap.add_argument("--indir", default="out", help="snapshot dir; populated automatically in fetch-latest mode")
    ap.add_argument("--fetch-latest", action="store_true", default=True, help="fetch latest data from Supabase (default)")
    ap.add_argument("--offline", action="store_true", help="reuse existing snapshots in --indir")
    ap.add_argument("--batch-date", default=None, help="scored batch report_date (default = newest scored batch)")
    ap.add_argument("--vdate", default=None, help="flow verdict_date used for evidence (default = newest)")
    ap.add_argument("--dp-date", default=None, help="darkpool fetch_date (default = vdate)")
    ap.add_argument("--nf-date", default=None, help="normalized flow signal_date (default = vdate)")
    ap.add_argument("--model-as-of", default=None, help="sigma vintage label (default: max sigma_as_of in batch rows)")
    ap.add_argument("--out", required=True, help="output markdown path")
    args = ap.parse_args()
    if not args.offline:
        from shortdate_data import fetch_latest
        st = fetch_latest(Path(args.indir))
        print("fetch-latest state:", {k: st[k] for k in ["batch_date", "vdate", "stldc_date", "quant2_run", "dp_date", "nf_date"] if k in st})
        if st.get("unscored_batches"):
            print(f"NOTE unscored newer batches (run em_score.py to score): {st['unscored_batches']}")
    if not (args.batch_date and args.vdate):
        st = json.loads((Path(args.indir) / "fetch_state.json").read_text(encoding="utf-8"))
        args.batch_date = args.batch_date or st["batch_date"]
        args.vdate = args.vdate or st["vdate"]
    if not args.model_as_of:
        em_all = load_creds_json(Path(args.indir) / "em_scores.json")
        sigs = [str(x.get("sigma_as_of"))[:10] for x in em_all if str(x.get("report_date"))[:10] == args.batch_date]
        args.model_as_of = max(sigs) if sigs else args.batch_date
    dp_date = args.dp_date or args.vdate
    nf_date = args.nf_date or args.vdate
    d = Path(args.indir)

    em = load_creds_json(d / "em_scores.json")
    davis = load_creds_json(d / "ledger_davis.json")
    uw = load_creds_json(d / "ledger_uw.json")
    recon = {r["symbol"]: r for r in load_creds_json(d / f"flow_reconcile_{args.vdate}.json")["reconciliation"]}
    stldc = {r["symbol"]: r for r in load_creds_json(d / "stldc_latest.json")}
    quant = {r["ticker"]: r for r in load_creds_json(d / "quant2_leaderboard.json")}
    dp = [x for x in load_creds_json(d / "darkpool_recent.json") if str(x.get("fetch_date"))[:10] == dp_date]
    dp_by = {x.get("symbol"): x for x in dp}
    nf = [x for x in load_creds_json(d / "normalized_flow_recent.json") if str(x.get("signal_date")) == nf_date]
    nf_by = {x.get("ticker"): x for x in nf}
    val = {}
    for x in load_creds_json(d / "valuation_full.json"):
        t = x.get("ticker")
        if t not in val:
            val[t] = x

    led = {}
    for x in davis + uw:
        key = (x.get("symbol"), str(x.get("expiry") or x.get("short_expiry")), float(x.get("short_strike") or 0))
        led.setdefault(key, x)

    scored = []
    for e in em:
        if str(e.get("report_date"))[:10] != args.batch_date:
            continue
        sym, strategy = str(e.get("symbol")), str(e.get("strategy"))
        fam = FAMILY.get(strategy)
        if fam is None:
            continue
        pool = davis if e.get("source_table") == "option-opportunity-davis" else uw
        L = next((x for x in pool if x.get("opportunity_key") == e.get("opportunity_key")), {})
        rec, st, q = recon.get(sym, {}), stldc.get(sym, {}), quant.get(sym, {})
        dp, nf = dp_by.get(sym, {}), nf_by.get(sym, {})
        v = val.get(sym, {})
        side = SIDE[fam]
        verdict, status = str(rec.get("verdict")), str(rec.get("status"))
        stg = str(st.get("stage") or "")
        stg_side = 1 if stg.startswith("BULL") else (-1 if stg.startswith("BEAR") else 0)
        fresh = str(st.get("run_date")) == args.vdate
        s = {}
        s["jlaw"] = 2.0 * side if str(L.get("jlaw_classification")) == "Watch" else 0.0
        if verdict == "bullish":
            s["flow"] = (0.5 if status == "confirmed" else 0.25) * side
        elif verdict == "bearish":
            mag = 3.0 if status == "confirmed" else 1.5
            s["flow"] = mag if side == -1 else -mag
        elif status == "conflict":
            s["flow"] = -2.0
        else:
            s["flow"] = 0.0
        if stg and side == stg_side:
            s["stldc"] = (1.5 if fresh else 0.75) if "CONFIRM" in stg else 0.0
        elif stg and side == -stg_side:
            s["stldc"] = -1.0 if "CONFIRM" in stg else -0.25
        else:
            s["stldc"] = 0.0
        dpn = n(dp.get("net_premium")) or 0.0
        if dpn >= 1e8:
            s["dp"] = 1.0 * side
        elif dpn > 0:
            s["dp"] = 0.5 * side
        elif dpn <= -1e8:
            s["dp"] = -1.0 * side
        elif dpn < 0:
            s["dp"] = -0.5 * side
        else:
            s["dp"] = 0.0
        z = n(nf.get("net_bias_z")) or 0.0
        s["uf"] = 1.0 * side if abs(z) >= 1 else (0.5 * side if abs(z) >= 0.5 else 0.0)
        qr = q.get("rank")
        s["q2"] = 1.0 if qr and qr <= 20 else (0.5 if qr and qr <= 100 else 0.0)
        credit_sh = n(L.get("entry_credit")) or 0.0  # davis & uw entry_credit are PER SHARE
        k = n(L.get("short_strike")) or 0
        spot = n(L.get("spot_price")) or 0
        width = None
        if fam == "BPS":
            width = k - (n(L.get("long_strike")) or 0)
        elif fam == "BCS":
            width = (n(L.get("long_strike")) or 0) - k
        maxloss_sh = ((width - credit_sh) if width is not None else None) if fam in ("BPS", "BCS") else (k if fam == "SP" else None)
        scored.append({
            "ticker": sym, "family": fam, "strategy": strategy,
            "legs": f"{k:g}" + (f"/{n(L.get('long_strike')):g}" if L.get("long_strike") else ""),
            "expiry": str(L.get("expiry") or L.get("short_expiry")), "dte": L.get("dte") or L.get("short_dte"),
            "pwin": e.get("p_win_expiry"), "p50": e.get("p_profit50_first"),
            "credit_sh": credit_sh, "maxloss_sh": maxloss_sh, "spot": spot,
            "conf": round(sum(s.values()), 2), "parts": s,
            "verdict": verdict, "status": status, "stage": stg or "—", "fresh": fresh,
            "jlaw": str(L.get("jlaw_classification") or "—"),
            "dpn": dpn, "z": z, "qr": qr,
            "fv_adj": v.get("fair_value_adj"), "bb_adj": v.get("buy_below_adj"),
            "ref_bb": v.get("reference_buy_below"), "ref_fv": v.get("reference_fair_value"),
            "ref_date": v.get("reference_valuation_date"), "val_cat": v.get("valuation_category"),
            "val_as_of": v.get("as_of_date"),
            "earnings": str(L.get("earnings_date") or "—"),
            "delta": L.get("short_delta"), "oi": L.get("short_oi"),
            "gex_wall": L.get("gex_put_wall"),
            "source": e.get("source_table"), "key": e.get("opportunity_key"),
            "sigma": str(e.get("sigma_as_of"))[:10],
            "quote_as_of": str(L.get("quote_as_of"))[:19], "quote_status": L.get("quote_status"),
        })

    md = [f"""## Part 4 · 期权计划 | Options Plan — 分策略 Top-5 置信度提案（研究用途）

<callout icon="📝" color="blue_bg"><b>4.1 分策略 Top-5（SP/SC/BPS/BCS）——置信度按六层证据加权</b>（jlaw · flow-verdict · unusual flow · dark pool · quant2 · stldc），权重标定自 win-rate 分级（session sess_bacf128e，窗口 2026-10-08→11-13 下跌市）：jlaw-yahoo Watch 61.6%/67.4% 唯一强正 edge；flow-verdict bullish 40.0% 低于 base、bearish 64.8%/75% 最强；STLDC CONFIRM chained 56.9% 薄 edge（fresh 57.1% > stale 53.5%）；quant2 未验证。EM pWin 为 σ={args.model_as_of} 当前 vintage 的模型测量。报价为 dated snapshot；入场前须独立重核 NBBO/事件/流动性/保证金。无订单、无写入。</callout>

### 4.0 评分口径 | Scoring rubric（结构方向对齐后计分，逆向取负）

<table fit-page-width="true" header-row="true">
<tr><th>证据层</th><th>权重依据（实测）</th><th>满分</th></tr>
<tr><td>jlaw Watch（多头侧）</td><td>yahoo Watch 61.6%/67.4% 最强；chart Watch 41.4% 反向 → 未分管道折中</td><td>+2.0 / −2.0</td></tr>
<tr><td>flow-verdict bearish（空头侧）confirmed/partial</td><td>64.8% 5d / 75% 10d（+12~28pts 最强空头证据）</td><td>+3.0 / +1.5</td></tr>
<tr><td>flow-verdict bullish（多头侧）confirmed/partial</td><td>40.0% 5d 低于 base → 仅小加</td><td>+0.5 / +0.25</td></tr>
<tr><td>flow-verdict conflict</td><td>不可作单向结论</td><td>−2.0</td></tr>
<tr><td>STLDC CONFIRM 顺势（fresh/stale）</td><td>chained 56.9%（+2.6pts）；fresh 57.1% vs stale 53.5%</td><td>+1.5 / +0.75</td></tr>
<tr><td>STLDC 逆势 CONFIRM / ALERT</td><td>BEAR-CONFIRM 46.3%（+0.6）；ALERT 无 edge</td><td>−1.0 / −0.25</td></tr>
<tr><td>dark pool 顺势 ≥$100M / 逆势</td><td>未独立分级 → 协同证据</td><td>±1.0 / ±0.5</td></tr>
<tr><td>unusual flow \\|z\\|≥1 顺势 / 逆势</td><td>未独立分级 → 协同证据</td><td>±1.0 / −1.0</td></tr>
<tr><td>quant2 rank≤20 / ≤100</td><td>probability_status=not_validated → 最小权重</td><td>+1.0 / +0.5</td></tr>
</table>
置信度 = 各层之和；SP/BPS 按多头证据、SC/BCS 按空头证据对齐。范围为 {args.batch_date} 已评分批；每策略取前 5，不足则全列。davis/uw `collateral` 字段为占位坏列：SP 保证金按 strike×100 计，BPS/BCS max loss = (宽 − credit)×100。"""]

    for fam, title in FAM_TITLES:
        pool = sorted([x for x in scored if x["family"] == fam], key=lambda x: -x["conf"])[:5]
        md.append(f"\n### 4.1.{FAM_IDX[fam]} {title} — Top {len(pool) if pool else 0}\n")
        md.append('<table fit-page-width="true" header-row="true">')
        md.append("<tr><th>#</th><th>Ticker</th><th>Strike(s)</th><th>Expiry/DTE</th><th>Confidence</th><th>pWin</th><th>p50</th><th>Credit/股</th><th>Max loss/张</th><th>jlaw</th><th>Flow(%s)</th><th>STLDC(%s)</th><th>DP net</th><th>z</th><th>quant2</th><th>来源</th></tr>" % (args.vdate, args.vdate))
        for i, x in enumerate(pool, 1):
            ml = f"${x['maxloss_sh']*100:,.0f}" if x["maxloss_sh"] else "实股口径"
            md.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s /%s</td><td>%.2f</td><td>%.1f%%</td><td>%s</td><td>$%s</td><td>%s</td><td>%s</td><td>%s / %s</td><td>%s</td><td>%+.1fM</td><td>%+.2f</td><td>%s</td><td>%s</td></tr>" % (
                i, x["ticker"], x["legs"], x["expiry"], x["dte"] if x["dte"] is not None else "?", x["conf"],
                x["pwin"] * 100, ("%.1f%%" % (x["p50"] * 100)) if x["p50"] is not None else "N/A",
                f"{x['credit_sh']:g}", ml, x["jlaw"], x["verdict"], x["status"],
                x["stage"], (x["dpn"] / 1e6), x["z"], ("#%d" % x["qr"]) if x["qr"] else "off",
                "davis" if x["source"].startswith("option") else "uw"))
        md.append("</table>")
        if not pool:
            md.append("无合格供给 None / 无。")
    md.append(f"""
### 4.2 边界 | Boundaries
- 置信度为证据加权相对排序，非胜率承诺；报价为 dated snapshot（per-row quote_as_of，最新 {max((x['quote_as_of'] for x in scored), default='n/a')} UTC）→ 入场前重询 NBBO。
- jlaw 分类未区分 yahoo/chart 管道（chart Watch 实测反向）→ 折中 ±2.0，是最大不确定项。
- 模型 pWin = 短腿到期 OTM 概率（σ={args.model_as_of}），非经验胜率。
- 研究分析非投资建议。无订单、无写入。
""")
    Path(args.out).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"part 4.1 markdown -> {args.out}")
    for fam, _ in FAM_TITLES:
        pool = sorted([x for x in scored if x["family"] == fam], key=lambda x: -x["conf"])[:5]
        print(fam, "->", [(x["ticker"], x["conf"]) for x in pool])


if __name__ == "__main__":
    main()
