"""EM win-probability scoring job (runs daily ~13:15 HKT, after the three
options workflows have upserted their boards).

For each source table it takes the LATEST report_date present, reads that
day's open proposals, joins the em-model-snapshot, computes
  - p_win_expiry      P(short leg finishes OTM), drift-off calibrated model
  - p_profit50_first  P(50% of credit banked before the short strike is touched)
  - outside_band112 / calib_cov_30d / earnings_in_window flags
and upserts to Supabase public."em-probability"
(PK source_table + opportunity_key, idempotent — safe to re-run).

Usage: python em_score.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import requests

from em_model import (  # noqa: E402
    DATA_DIR as OUT,
    MODEL_VERSION,
    load_creds,
    outside_band112,
    p_profit50_first,
    p_win_expiry,
    sigma50,
)

# per-table field adapters; credit must end up PER SHARE
ADAPTERS = {
    "uw-option-opportunities": {
        "status_col": "status", "status_val": "PROPOSED",
        "strategy": "strategy", "bucket": "dte_bucket",
        "expiry": "short_expiry", "spot": "spot_price", "strike": "short_strike",
        "opt_sym": "short_option_symbol", "delta": "short_delta",
        "credit": "entry_credit", "earn": "earnings_date", "side": None,
    },
    "option-opportunity-davis": {
        "status_col": "status", "status_val": "PROPOSED",
        "strategy": "strategy", "bucket": None,
        "expiry": "expiry", "spot": "spot_price", "strike": "short_strike",
        "opt_sym": "short_option_symbol", "delta": "short_delta",
        "credit": "entry_credit", "earn": "earnings_date", "side": "side",
    },
    "csp-bps-opportunity": {
        "status_col": "candidate_status", "status_val": "ELIGIBLE",
        "strategy": "strategy", "bucket": None,
        "expiry": "expiry", "spot": "spot_price", "strike": "short_strike",
        "opt_sym": "short_option_symbol", "delta": "short_delta",
        "credit": "short_bid",  # table's entry_credit is contract dollars; bid is per share
        "earn": "earnings_date", "side": None,
    },
}


def sb_get(url: str, key: str, path: str) -> list:
    r = requests.get(f"{url}/rest/v1/{path}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def load_snapshot(url: str, key: str) -> dict[str, dict]:
    rows = sb_get(url, key, "em-model-snapshot?select=*&order=as_of.desc&limit=3000")
    snap: dict[str, dict] = {}
    for r in rows or []:
        sym = str(r["symbol"]).upper()
        if sym not in snap:  # first hit per symbol = highest as_of
            snap[sym] = r
    return snap


def is_put_of(row: dict, adapter: dict) -> bool:
    if adapter["side"] and row.get(adapter["side"]):
        return str(row[adapter["side"]]).lower().startswith("put")
    sym = str(row.get(adapter["opt_sym"]) or "")
    return len(sym) >= 9 and sym[-9:-8].upper() == "P"


def per_share_credit(row: dict, adapter: dict) -> float:
    val = row.get(adapter["credit"]) if adapter["credit"] else None
    if val is None:
        return 0.0
    val = float(val)
    if adapter["credit"] == "entry_credit" and "short_bid" in row and row.get("short_bid"):
        return float(row["short_bid"])  # csp-bps contract dollars -> per share bid
    return val / 100.0 if val > 25 else val  # guard against contract-dollar fields


def main() -> None:
    url, key = load_creds()
    snap = load_snapshot(url, key)
    print(f"Model snapshot: {len(snap)} symbols (version {MODEL_VERSION})")

    # local fallbacks for symbols the snapshot missed
    closes = pd.read_pickle(os.path.join(OUT, "em_closes.pkl")) if os.path.exists(
        os.path.join(OUT, "em_closes.pkl")) else {}
    cov_path = os.path.join(OUT, "em_coverage_by_ticker.csv")
    calib = {}
    if os.path.exists(cov_path):
        cov = pd.read_csv(cov_path)
        cov = cov[cov.n_obs >= 12]
        calib = cov.set_index("ticker")[["cov_off", "n_obs"]].apply(tuple, axis=1).to_dict()

    out_rows: list[dict] = []
    for table, ad in ADAPTERS.items():
        latest = sb_get(url, key, f"{table}?select=report_date&order=report_date.desc&limit=1")
        if not latest:
            print(f"{table}: no rows at all, skipping")
            continue
        rep = str(latest[0]["report_date"])[:10]
        rows = sb_get(url, key,
                      f"{table}?select=*&{ad['status_col']}=eq.{ad['status_val']}"
                      f"&report_date=eq.{rep}")
        scored = 0
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if not sym or sym == "--":
                continue
            s0 = float(row.get(ad["spot"]) or 0)
            k = float(row.get(ad["strike"]) or 0)
            if s0 <= 0 or k <= 0:
                continue
            exp = str(row.get(ad["expiry"]) or "")[:10]
            if not exp:
                continue
            rep_d = date.fromisoformat(rep)
            exp_d = date.fromisoformat(exp)
            h = max(1, int(np.busday_count(rep_d, exp_d)))
            put = is_put_of(row, ad)
            delta = abs(float(row.get(ad["delta"]) or 0.0))
            credit = per_share_credit(row, ad)
            earn = str(row.get(ad["earn"]) or "")[:10]

            s = snap.get(sym)
            if s is not None:
                sigma = float(s["sigma_daily"])
                cov30 = s.get("calib_cov_30d")
                as_of = s["as_of"]
            elif sym in closes:  # fallback: local cache
                sigma = sigma50(closes[sym])
                c = calib.get(sym)
                cov30, as_of = (c[0] / 100.0 if c else None), str(
                    closes[sym].dropna().index[-1].date())
            else:
                print(f"  {table} {sym}: no sigma available, skipped")
                continue

            p_exp = p_win_expiry(s0, k, h, put, sigma)
            p50 = p_profit50_first(s0, k, delta, credit)
            out_rows.append({
                "source_table": table,
                "opportunity_key": row.get("opportunity_key"),
                "report_date": rep,
                "symbol": sym,
                "strategy": row.get(ad["strategy"]),
                "bucket": row.get(ad["bucket"]) if ad["bucket"] else row.get("dte"),
                "spot": round(s0, 2),
                "strike": k,
                "option_type": "put" if put else "call",
                "expiry": exp,
                "h_trading_days": h,
                "credit": round(credit, 4),
                "delta": round(delta, 4),
                "p_win_expiry": round(p_exp, 4),
                "p_profit50_first": round(p50, 4) if p50 is not None else None,
                "outside_band112": outside_band112(s0, k, h, put, sigma),
                "calib_cov_30d": round(float(cov30), 4) if cov30 is not None else None,
                "earnings_in_window": bool(earn and rep < earn <= exp),
                "model_version": MODEL_VERSION,
                "sigma_as_of": as_of,
            })
            scored += 1
        print(f"{table}: report_date={rep}, scored {scored}/{len(rows)} "
              f"{ad['status_col']}={ad['status_val']} rows")

    if not out_rows:
        print("Nothing to score.")
        return
    df = pd.DataFrame(out_rows).sort_values("p_win_expiry")
    df.to_csv(os.path.join(OUT, f"em_scored_{date.today()}.csv"), index=False)

    import json as _json
    r = requests.post(
        f"{url}/rest/v1/em-probability?on_conflict=source_table,opportunity_key",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=representation"},
        data=_json.dumps(out_rows),  # python natives; avoids pandas float-coercion
        timeout=60,
    )
    if r.status_code in (200, 201):
        print(f"Upserted {len(r.json())} rows -> public.em-probability")
    else:
        print(f"UPSERT FAILED {r.status_code}: {r.text[:300]}")
        sys.exit(1)

    print(f"\n{'sym':6s} {'table':12s} {'strategy':17s} {'stk':>8s} {'h':>3s} "
          f"{'pExp':>6s} {'p50':>6s} {'1.12sd':>6s} {'calib':>6s} {'earn':>5s}")
    for _, x in df.iterrows():
        p50s = f"{100 * x.p_profit50_first:6.1f}" if pd.notna(x.p_profit50_first) else "   -  "
        print(f"{x.symbol:6s} {x.source_table.replace('-opportunity', ''):12s} "
              f"{str(x.strategy):17s} {x.strike:8.1f} {x.h_trading_days:3d} "
              f"{100 * x.p_win_expiry:6.1f} {p50s} "
              f"{'OUT' if x.outside_band112 else 'in':>6s} "
              f"{round(100 * x.calib_cov_30d) if pd.notna(x.calib_cov_30d) else '-':>6} "
              f"{'YES' if x.earnings_in_window else '-':>5s}")


if __name__ == "__main__":
    main()
