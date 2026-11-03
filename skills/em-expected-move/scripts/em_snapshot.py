"""EM model snapshot job (runs daily ~11:30 HKT, before the options workflows).

1. Refresh the local bars cache (out/em_closes.pkl) incrementally:
   watchlist symbols (all categories) + any symbols seen in the last few
   report dates of the three proposal tables (davis/manus boards can cover
   names outside the watchlist).
2. Compute the per-symbol model snapshot: last close, sigma(50),
   calib coverage from the weekly backtest (out/em_coverage_by_ticker.csv).
3. Upsert to Supabase public."em-model-snapshot" (PK symbol+as_of, idempotent).

Usage: python em_snapshot.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import requests

from em_backtest import download_close, fetch_watchlist, update_recent_bars
from em_model import DATA_DIR as OUT, MODEL_VERSION, load_creds, sigma50

PROPOSAL_TABLES = ("uw-option-opportunities", "option-opportunity-davis", "csp-bps-opportunity")


def recent_proposal_symbols(url: str, key: str, days: int = 6) -> set[str]:
    """Symbols on recent report dates of the three proposal tables."""
    syms: set[str] = set()
    for table in PROPOSAL_TABLES:
        try:
            r = requests.get(
                f"{url}/rest/v1/{table}?select=symbol&order=report_date.desc&limit=1500",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                timeout=30,
            )
            rows = r.json()
            if isinstance(rows, list):
                syms |= {str(x["symbol"]).upper() for x in rows if x.get("symbol")}
        except Exception as exc:  # one table failing must not kill the snapshot
            print(f"  warn: {table} symbol fetch failed: {exc}")
    return syms


def main() -> None:
    url, key = load_creds()
    buckets = fetch_watchlist()
    watch_syms = sorted({s for syms in buckets.values() for s in syms})
    extra = recent_proposal_symbols(url, key)
    wanted = sorted(set(watch_syms) | extra)
    print(f"Snapshot universe: {len(wanted)} symbols "
          f"(watchlist {len(watch_syms)} + {len(set(wanted) - set(watch_syms))} from proposal boards)")

    cache = os.path.join(OUT, "em_closes.pkl")
    closes = pd.read_pickle(cache) if os.path.exists(cache) else {}
    fresh = [s for s in wanted if s not in closes]
    if fresh:
        print(f"Downloading bars for {len(fresh)} new symbols ...")
        closes.update(download_close(fresh, years=5))
    known = [s for s in wanted if s in closes]
    if known:
        print(f"Refreshing recent bars for {len(known)} cached symbols ...")
        n_upd = update_recent_bars(closes, known)
        print(f"  extended {n_upd} series with new sessions")
    pd.to_pickle(closes, cache)

    cov_path = os.path.join(OUT, "em_coverage_by_ticker.csv")
    calib: dict[str, tuple[float, int]] = {}
    if os.path.exists(cov_path):
        cov = pd.read_csv(cov_path)
        cov = cov[cov.n_obs >= 12]
        calib = cov.set_index("ticker")[["cov_off", "n_obs"]].apply(tuple, axis=1).to_dict()

    rows = []
    for sym, ser in closes.items():
        ser = ser.dropna()
        if len(ser) < 52 or ser.iloc[-1] <= 0:
            continue
        sd = sigma50(ser)
        if not sd or sd <= 0:
            continue
        c, n = calib.get(sym, (None, None))
        rows.append({
            "symbol": sym,
            "as_of": str(ser.index[-1].date()),
            "close": round(float(ser.iloc[-1]), 4),
            "sigma_daily": round(sd, 6),
            "calib_cov_30d": round(float(c) / 100.0, 4) if c is not None else None,
            "calib_n": int(n) if n is not None else None,
            "model_version": MODEL_VERSION,
        })
    print(f"Computed snapshot for {len(rows)} symbols")

    import json as _json
    payload = _json.dumps(rows)  # python natives only; avoids pandas float-coercion
    r = requests.post(
        f"{url}/rest/v1/em-model-snapshot?on_conflict=symbol,as_of",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=representation"},
        data=payload,
        timeout=60,
    )
    if r.status_code in (200, 201):
        print(f"Upserted {len(r.json())} rows -> public.em-model-snapshot")
    else:
        print(f"UPSERT FAILED {r.status_code}: {r.text[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
