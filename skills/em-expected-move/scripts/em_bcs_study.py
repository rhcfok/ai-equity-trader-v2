"""EM pricing study for vertical spreads (BCS / BPS) — ad-hoc CLI.

Computes, from the calibrated drift-off lognormal (single source: em_model.py):
  - three-layer outcome probabilities: full win / partial / max loss
  - model fair net credit (realized-vol basis) and model |delta| of the short leg
  - breakeven probability at the actual credit (if --credit given) and at
    fair / fair+0.5 / fair+1.0
  - 1.12-sigma band placement and per-symbol calib coverage from the snapshot
    table when the symbol has one

Data: fresh yfinance daily bars for the symbol (spot + sigma from the same
series). Works for any ticker, including ones outside the watchlist (they get
calib_cov_30d = None — see SKILL.md limitations).

Usage (repo root):
  python skills/em-expected-move/scripts/em_bcs_study.py \
      --symbol SPY --short 775 --long 785 --expiry 2026-11-20
  python ... --symbol CRM --short 240 --long 250 --expiry 2026-11-20 \
      --credit 3.10 [--side put] [--report-date 2026-11-03] [--earnings-date 2026-12-02]
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import yfinance as yf

from em_model import (  # noqa: E402
    Z68,
    bcs_fair_credit,
    load_creds,
    model_delta,
    outside_band112,
    p_profit50_first,
    p_win_expiry,
    sigma50,
)


def fetch_bars(symbol: str):
    df = yf.download(symbol, period="1y", interval="1d", auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise SystemExit(f"no bars returned for {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # MultiIndex columns -> one ticker column
        close = close.iloc[:, 0]
    return close.dropna().astype(float)


def snapshot_row(symbol: str) -> dict | None:
    try:
        url, key = load_creds()
        import requests
        r = requests.get(
            f"{url}/rest/v1/em-model-snapshot?select=*&symbol=eq.{symbol}"
            f"&order=as_of.desc&limit=1",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=15,
        )
        rows = r.json()
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception as exc:
        print(f"  (snapshot lookup skipped: {exc})")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="EM vertical-spread pricing study")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--short", type=float, required=True, help="short strike")
    ap.add_argument("--long", type=float, required=True, help="long strike")
    ap.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    ap.add_argument("--side", choices=("call", "put"), default="call",
                    help="call = bear call spread (default), put = bull put spread")
    ap.add_argument("--credit", type=float, help="actual net credit per share (optional)")
    ap.add_argument("--report-date", default=str(date.today()))
    ap.add_argument("--earnings-date", default=None, help="YYYY-MM-DD, if known")
    args = ap.parse_args()

    is_put = args.side == "put"
    rep = date.fromisoformat(args.report_date)
    exp = date.fromisoformat(args.expiry)
    if exp <= rep:
        raise SystemExit("expiry must be after report date")
    h = max(1, int(np.busday_count(rep, exp)))

    ser = fetch_bars(args.symbol)
    s0 = float(ser.iloc[-1])
    sd = sigma50(ser)
    as_of = str(ser.index[-1].date())
    vol_h = sd * math.sqrt(h)

    k_s, k_l = args.short, args.long
    if (not is_put and not (k_s < k_l)) or (is_put and not (k_l < k_s)):
        raise SystemExit("strike order wrong for the chosen side "
                         "(BCS: short < long calls; BPS: long < short puts)")

    # three-layer outcome probabilities
    if is_put:
        p_win = p_win_expiry(s0, k_s, h, True, sd)          # P(S_T >= short)
        p_max = 1 - p_win_expiry(s0, k_l, h, True, sd)      # P(S_T < long)
    else:
        p_win = p_win_expiry(s0, k_s, h, False, sd)         # P(S_T <= short)
        p_max = 1 - p_win_expiry(s0, k_l, h, False, sd)     # P(S_T > long)
    p_part = 1 - p_win - p_max

    fair = bcs_fair_credit(s0, k_s, k_l, h, is_put, sd)
    delta = model_delta(s0, k_s, h, is_put, sd)
    band_edge = s0 * math.exp(Z68 * vol_h)

    snap = snapshot_row(args.symbol)
    snap_calib = None
    if snap and snap.get("calib_cov_30d") is not None:
        snap_calib = float(snap["calib_cov_30d"])

    print(f"\n=== {args.symbol} {'BULL_PUT' if is_put else 'BEAR_CALL'} SPREAD "
          f"{k_s}/{k_l} exp {exp} (h={h} trd) ===")
    print(f"spot {s0:.2f} (yfinance close {as_of})  sigma50 {100 * sd:.2f}%/d  "
          f"vol_h {100 * vol_h:.1f}%  1.12sd edge {band_edge:.0f}")
    print(f"P(full win)   = {100 * p_win:5.1f}%")
    print(f"P(partial)    = {100 * p_part:5.1f}%")
    print(f"P(max loss)   = {100 * p_max:5.1f}%")
    print(f"model fair credit = {fair:.2f}/share ({100 * fair:.0f}/contract); "
          f"short-leg model |delta| = {delta:.3f}")
    print(f"short strike beyond 1.12sd band: {outside_band112(s0, k_s, h, is_put, sd)}")
    if snap:
        calib_txt = (f"{100 * snap_calib:.0f}%" if snap_calib is not None
                     else "None (no watchlist history)")
        print(f"snapshot: as_of {snap['as_of']}  sigma {100 * float(snap['sigma_daily']):.2f}%/d  "
              f"calib_cov_30d {calib_txt}")
    else:
        print("snapshot: no row (symbol outside watchlist/boards) -> calib None, "
              "probability has no historical calibration backing")
    earn = args.earnings_date
    if earn and rep < date.fromisoformat(earn) <= exp:
        print("WARNING: earnings inside the window -> model probabilities INVALID")

    def p_be(credit: float) -> float:
        k_be = k_s + credit if not is_put else k_s - credit
        return p_win_expiry(s0, k_be, h, is_put, sd)

    print("breakeven probabilities:")
    for label, c in (("at fair", fair), ("fair+0.5", fair + 0.5), ("fair+1.0", fair + 1.0)):
        print(f"  {label:8s} credit {c:5.2f} -> P(S_T < BE) = {100 * p_be(c):5.1f}%")
    if args.credit is not None:
        c = args.credit
        p50 = p_profit50_first(s0, k_s, delta, c)
        print(f"AT ACTUAL CREDIT {c:.2f}: P(S_T < BE) = {100 * p_be(c):.1f}%   "
              f"P(profit50 first) = "
              f"{f'{100 * p50:.1f}%' if p50 is not None else 'n/a'}   "
              f"richness vs model = {c - fair:+.2f}/share")
    print("caveats: realized-vol basis (a vol regime shift invalidates); Gaussian "
          "tails optimistic (30d coverage 64.5%); upside breaches ~1.4x downside at 1sd.")


if __name__ == "__main__":
    main()
