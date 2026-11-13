#!/usr/bin/env python3
"""Short-date top-10 proposal (valuation = REFERENCE ONLY; flow-conflict / bearish-vs-put gates).

Fetches the latest of everything from Supabase (read-only) via shortdate_data.fetch_latest,
then ranks the newest SCORED batch by em-probability p_win_expiry with per-row due-diligence:
flow verdict (conflict / bearish-vs-put veto), STLDC stage, quant2 leaderboard, valuation
reference columns (fair_value/buy_below + Rijnberk reference_* — NOT a veto).

Run order: run skills/em-expected-move/scripts/em_score.py first if a newer batch is unscored
(this script falls back to the newest scored batch and tells you).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from shortdate_data import fetch_latest

OUT = None
BATCH = None


def n(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> None:
    global OUT, BATCH
    ap = argparse.ArgumentParser(description="short-date top-10 (read-only)")
    ap.add_argument("--indir", default="out", help="snapshot dir (auto-populated)")
    ap.add_argument("--offline", action="store_true", help="reuse existing snapshots")
    ap.add_argument("--batch-date", default=None)
    ap.add_argument("--vdate", default=None)
    ap.add_argument("--out", default=None, help="output json path (default <indir>/top10_reference_<batch>.json)")
    args = ap.parse_args()
    indir = Path(args.indir)
    st = None
    if not args.offline:
        st = fetch_latest(indir)
        print("fetch-latest state:", {k: st[k] for k in ["batch_date", "vdate", "stldc_date", "quant2_run", "dp_date", "nf_date"] if k in st})
        if st.get("unscored_batches"):
            print(f"NOTE unscored newer batches (run em_score.py, then re-run): {st['unscored_batches']}")
        if st.get("darkpool_duplicate_of_prior_day"):
            print("NOTE darkpool latest snapshot duplicates prior day (DP treated as missing evidence)")
    if not (args.batch_date and args.vdate):
        stf = json.loads((indir / "fetch_state.json").read_text(encoding="utf-8"))
        args.batch_date = args.batch_date or stf["batch_date"]
        args.vdate = args.vdate or stf["vdate"]
    BATCH = args.batch_date
    OUT = indir
    vdate = args.vdate

    davis = json.loads((indir / "ledger_davis.json").read_text(encoding="utf-8"))
    uw = json.loads((indir / "ledger_uw.json").read_text(encoding="utf-8"))
    em = json.loads((indir / "em_scores.json").read_text(encoding="utf-8"))
    recon = {r["symbol"]: r for r in json.loads((indir / f"flow_reconcile_{vdate}.json").read_text(encoding="utf-8"))["reconciliation"]}
    stldc = {r["symbol"]: r for r in json.loads((indir / "stldc_latest.json").read_text(encoding="utf-8"))}
    quant = {r["ticker"]: r for r in json.loads((indir / "quant2_leaderboard.json").read_text(encoding="utf-8"))}
    dp_rows = json.loads((indir / "darkpool_recent.json").read_text(encoding="utf-8"))
    nf_rows = json.loads((indir / "normalized_flow_recent.json").read_text(encoding="utf-8"))
    dp_by = {x.get("symbol"): x for x in dp_rows}
    nf_by = {x.get("ticker"): x for x in nf_rows}
    val = {}
    for x in json.loads((indir / "valuation_full.json").read_text(encoding="utf-8")):
        t = x.get("ticker")
        if t not in val:
            val[t] = x

    em_by_key = {(e.get("source_table"), e.get("opportunity_key")): e for e in em
                 if str(e.get("report_date"))[:10] == BATCH}

    rows, pending = [], []
    for table, ledger in [("option-opportunity-davis", davis), ("uw-option-opportunities", uw)]:
        for r in ledger:
            sym, strategy = str(r.get("symbol")), str(r.get("strategy"))
            rec, stg_r, q = recon.get(sym, {}), stldc.get(sym, {}), quant.get(sym, {})
            dp, nf = dp_by.get(sym, {}), nf_by.get(sym, {})
            v = val.get(sym, {})
            base = {
                "ticker": sym, "strategy": strategy, "source_table": table,
                "rd": str(r.get("report_date")), "opportunity_key": r.get("opportunity_key"),
                "expiry": str(r.get("expiry") or r.get("short_expiry")),
                "dte": r.get("dte") or r.get("short_dte"),
                "short_strike": r.get("short_strike"), "long_strike": r.get("long_strike"),
                "spot": r.get("spot_price"), "credit": r.get("entry_credit"),
                "quote_as_of": str(r.get("quote_as_of"))[:19], "quote_status": r.get("quote_status"),
                "verdict": rec.get("verdict"), "flow_status": rec.get("status"),
                "composite": rec.get("flow_composite"), "stldc_stage": stg_r.get("stage"),
                "fv": v.get("fair_value"), "fv_adj": v.get("fair_value_adj"),
                "bb": v.get("buy_below"), "bb_adj": v.get("buy_below_adj"),
                "val_as_of": v.get("as_of_date"), "val_category": v.get("valuation_category"),
                "ref_source": v.get("reference_source"), "ref_bb": v.get("reference_buy_below"),
                "ref_fv": v.get("reference_fair_value"), "ref_date": v.get("reference_valuation_date"),
                "earnings": r.get("earnings_date"),
                "quant_rank": q.get("rank"), "quant_score": q.get("score_hist_freq"),
            }
            m = em_by_key.get((table, r.get("opportunity_key")))
            if m is None:
                pending.append(base)
                continue
            base["pwin"] = m.get("p_win_expiry")
            base["p50"] = m.get("p_profit50_first")
            base["sigma"] = str(m.get("sigma_as_of"))[:10]
            veto = None
            if base["flow_status"] == "conflict":
                veto = f"flow conflict ({base['composite']})"
            elif base["verdict"] == "bearish" and strategy in ("SHORT_PUT", "BULL_PUT_SPREAD", "CSP", "WHEEL_CSP"):
                veto = f"flow bearish ({base['composite']}) vs put-side"
            base["veto"] = veto
            rows.append(base)

    eligible = sorted([r for r in rows if not r["veto"]], key=lambda x: -x["pwin"])
    excluded = sorted([r for r in rows if r["veto"]], key=lambda x: -x["pwin"])
    top10 = eligible[:10]
    outp = Path(args.out or (indir / f"top10_reference_{BATCH}.json"))
    json.dump({"top10": top10, "eligible_below_cut": eligible[10:], "excluded": excluded,
               "pending_em": pending, "batch_date": BATCH, "vdate": vdate},
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"batch {BATCH} | scored {len(rows)} | eligible {len(eligible)} | vetoed {len(excluded)} | pending EM {len(pending)}")
    for i, r in enumerate(top10, 1):
        strat = {"SHORT_PUT": "SP", "BULL_PUT_SPREAD": "BPS", "BEAR_CALL_SPREAD": "BCS",
                 "WHEEL_CSP": "WCSP", "WHEEL_CC": "WCC"}.get(r["strategy"], r["strategy"])
        legs = str(r["short_strike"]) + ("/" + str(r["long_strike"]) if r["long_strike"] else "")
        print(f"{i:>2}. {r['ticker']:<5} {strat:<5} {legs:<12} exp {r['expiry']} dte {r['dte']} "
              f"pWin {r['pwin']:.1%} | flow {r['verdict']}/{r['flow_status']} | STLDC {r['stldc_stage'] or '—'} "
              f"| q2 {(f'#{r[chr(113)+chr(117)+chr(97)+chr(110)+chr(116)+chr(95)+chr(114)+chr(97)+chr(110)+chr(107)]}') if r['quant_rank'] else 'off'} | src {r['source_table'][:4]}")
    if excluded:
        print("vetoed:", [(r["ticker"], r["veto"]) for r in excluded])
    if pending:
        print("pending EM:", sorted({p["ticker"] for p in pending}))


if __name__ == "__main__":
    main()
