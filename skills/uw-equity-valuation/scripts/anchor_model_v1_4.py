"""Rijnberk-replica valuation anchor model, v1.4 (reference implementation).

Implements the anchor methodology documented in ../SKILL.md
("Valuation Anchors, calculation_version >= uw_rest_v1.4.0"):

    justified_mult = min(1.0 * min(fy2_growth, 2 * sector_median_growth) * 100, 30)
    hist_cap       = pe_5y_avg * 0.9                (ignored when > 300: GAAP artifacts)
    floor          = 0 if cyclical_peak_risk else sector_median_fy2_pe
    exit_mult      = clamp(justified_mult, floor, min(30, max(hist_cap, floor)))
    target         = exit_mult * fy2_eps
    fair_value     = target / 1.15 ** 2.25          (15%/yr required return)
    buy_below      = 0.70 * fair_value
    fair_value_adj = exit_mult * (fy2_eps + sbc_ttm / shares) / 1.15 ** 2.25   (fy2_adj <= 3 * fy2_eps)
    anchor_verdict = accumulate | watch | expensive | no_anchor

Calibration (2026-11-08, vs public.equity_valuation reference rows):
    TSM  fair 348 vs 350, buy_below 244 vs 230
    AVGO buy_below 392 vs 390
    TXN  fair 150 vs 150

Input:  JSON list of rows with keys
        ticker, price, fy1_eps, fy2_eps, sector, sbc_ttm, shares,
        pe_5y_avg, pe_5y_pctile, flags (list), ref_fair_value, ref_buy_below
        (sector medians computed internally from the same pull, n >= 4 else global fallback)
Output: same rows plus exit_mult, fair_value, buy_below, fair_value_adj, buy_below_adj,
        anchor_verdict, upside_bb, model params echoed in scenario_assumptions.

Usage:  python -I anchor_model_v1_4.py <input.json> <output.json>
"""
import json
import statistics
import sys
from collections import defaultdict

DISC = 1.15 ** 2.25      # 15%/yr required return over ~2.25 years
BB_RATIO = 0.70
EXIT_CAP = 30.0
PE5Y_BASIS_ADJ = 0.9     # 5-yr avg P/E is trailing-based; forward multiples run ~10% lower
SCEN = {
    "model": "rijnberk_replica", "version": "v1.4.0",
    "discount_rate": 0.15, "horizon_years": 2.25, "buy_below_ratio": BB_RATIO,
    "exit_cap": EXIT_CAP, "pe5y_basis_adj": PE5Y_BASIS_ADJ,
    "sector_median_source": "wind_consensus_peers",
}


def compute(rows):
    # sector medians from positive-EPS consensus names in the same pull
    by_sector = defaultdict(list)
    for r in rows:
        if r.get("fy1_eps") and r.get("fy2_eps") and r["fy1_eps"] > 0 and r["fy2_eps"] > 0:
            g = r["fy2_eps"] / r["fy1_eps"] - 1
            by_sector[r.get("sector") or "Unknown"].append((g, r["price"] / r["fy2_eps"]))
    med = {s: {"g": statistics.median([v[0] for v in vs]),
               "pe2": statistics.median([v[1] for v in vs]), "n": len(vs)}
           for s, vs in by_sector.items()}
    all_g = [v[0] for vs in by_sector.values() for v in vs]
    all_p = [v[1] for vs in by_sector.values() for v in vs]
    gmed = {"g": statistics.median(all_g), "pe2": statistics.median(all_p), "n": len(all_g)}

    out = []
    for r in rows:
        r = dict(r)
        flags = list(r.get("flags") or [])
        fy1, fy2 = r.get("fy1_eps"), r.get("fy2_eps")
        price = r.get("price")
        scen = dict(SCEN)
        if not (fy1 and fy2) or fy1 <= 0 or fy2 <= 0:
            if fy1 is not None and fy1 <= 0 and "negative_consensus_eps" not in flags:
                flags.append("negative_consensus_eps")
            if "no_valuation_anchor" not in flags:
                flags.append("no_valuation_anchor")
            r.update({"flags": flags, "scenario_assumptions": scen, "anchor_verdict": "no_anchor"})
            out.append(r)
            continue
        sm = med.get(r.get("sector") or "Unknown")
        if not sm or sm["n"] < 4:
            sm, flags = gmed, flags + ([] if "peer_set_small" in flags else ["peer_set_small"])
        g = fy2 / fy1 - 1
        justified = min(1.0 * min(g, 2 * sm["g"]) * 100, EXIT_CAP)
        pe5 = r.get("pe_5y_avg")
        hist_cap = pe5 * PE5Y_BASIS_ADJ if pe5 and pe5 < 300 else None
        cyclical = "cyclical_peak_risk" in flags
        if price / fy1 < 12 and not cyclical:
            flags.append("cyclical_peak_risk")
            cyclical = True
        floor = 0.0 if cyclical else sm["pe2"]
        eff_cap = min(EXIT_CAP, max(hist_cap, floor)) if hist_cap else EXIT_CAP
        exit_mult = max(floor, min(justified, eff_cap))
        fair = exit_mult * fy2 / DISC
        bb = BB_RATIO * fair
        r.update({
            "exit_mult": round(exit_mult, 1), "fair_value": round(fair, 0),
            "buy_below": round(bb, 0), "target": round(exit_mult * fy2, 0),
            "pe_5y_avg": pe5, "pe_5y_pctile": r.get("pe_5y_pctile"),
            "growth_fy2": round(g, 3), "flags": flags,
            "scenario_assumptions": scen, "sector_median_fy2_pe": round(sm["pe2"], 1),
            "sector_median_growth": round(sm["g"], 3), "n_peers": sm["n"],
        })
        sbc_ps = (r.get("sbc_ttm") / r.get("shares")) if (r.get("sbc_ttm") and r.get("shares")) else 0.0
        if sbc_ps > 0 and (fy2 + sbc_ps) <= 3 * fy2:
            fa = exit_mult * (fy2 + sbc_ps) / DISC
            r["fair_value_adj"] = round(fa, 0)
            r["buy_below_adj"] = round(BB_RATIO * fa, 0)
        if g > 2 * sm["g"] and "growth_consensus_dependent" not in flags:
            flags.append("growth_consensus_dependent")
            r["flags"] = flags
        r["anchor_verdict"] = ("accumulate" if price <= bb
                               else "watch" if price <= fair else "expensive")
        r["upside_bb"] = round((bb / price - 1) * 100, 0)
        ref_bb = r.get("ref_buy_below")
        if ref_bb and ref_bb > 0:
            r["buy_below_final"] = round(min(bb, ref_bb), 0)   # conservative composition
        out.append(r)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    rows = json.load(open(sys.argv[1], encoding="utf-8"))
    json.dump(compute(rows), open(sys.argv[2], "w", encoding="utf-8"), indent=0)
    print(f"anchors written: {sys.argv[2]}")
