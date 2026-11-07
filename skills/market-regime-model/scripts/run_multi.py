"""
run_multi.py - apply the SPY-calibrated regime model to SPY / QQQ / SOXX.

Protocol: all model parameters are FROZEN at the SPY-tuned values
(w=40, k=3, theta=0.55, m_conf=3, trend mom=126, band=2%) and transferred
unchanged. This is the honest test of parameter transfer. A per-ticker
trend-parameter grid is also produced as a robustness check (CSV only).

Per ticker outputs (out/<TICKER>/): fig2/fig3/fig5 + CSVs.
Cross-asset outputs (out/): fig6_cross_asset.png, summary_multi.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")

from run_all import COLORS, CANON, regime_bands, plain_log_axis, fmt_date, prob_cols
from pipeline import (load_spy, log_returns, walk_forward_windows,
                      walk_forward_hmm, transition_events, hybrid_regime,
                      KNOWN_EVENTS)
from backtest import backtest, perf_stats, drawdown_episodes, detection_lags

OUTM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
TICKERS = ["SPY", "QQQ", "SOXX"]
FROZEN = dict(w=40, k=3, theta=0.55, m_conf=3, mom=126, band=0.02)


def run_ticker(tkr: str) -> dict:
    t0 = time.time()
    outdir = os.path.join(OUTM, tkr)
    os.makedirs(outdir, exist_ok=True)
    print(f"[{tkr}] loading ...", flush=True)
    close = load_spy(cache_path=os.path.join(outdir, f"{tkr.lower()}_daily.csv"),
                     ticker=tkr)["Close"]
    r = log_returns(close)

    print(f"[{tkr}] walk-forward (frozen SPY params) ...", flush=True)
    oos_wk = walk_forward_windows(r, w=FROZEN["w"], k=FROZEN["k"],
                                  theta=FROZEN["theta"], m_conf=FROZEN["m_conf"])
    oos_hmm = walk_forward_hmm(r, k=FROZEN["k"], theta=FROZEN["theta"],
                               m_conf=FROZEN["m_conf"])
    hyb = hybrid_regime(oos_wk, close, mom=FROZEN["mom"], band=FROZEN["band"])
    oos_wk.to_csv(os.path.join(outdir, "regime_wk_oos.csv"))
    hyb.to_csv(os.path.join(outdir, "regime_hybrid_oos.csv"))

    trans_hyb = transition_events(hyb)
    trans_hyb.to_csv(os.path.join(outdir, "transitions_hybrid.csv"), index=False)

    # detection vs this ticker's own drawdown episodes
    episodes = drawdown_episodes(close, threshold=-0.15, early=-0.10)
    episodes = episodes[episodes["trough_date"] >= oos_wk.index[0]]
    lags = {}
    for nm, reg in [("wk", oos_wk["regime"]), ("hmm", oos_hmm["regime"]),
                    ("hybrid", hyb)]:
        lg = detection_lags(reg, episodes)
        lg.to_csv(os.path.join(outdir, f"detection_lags_{nm}.csv"), index=False)
        lags[nm] = lg
    print(f"[{tkr}] episodes>=15%: {len(episodes)} | hybrid detected "
          f"{(lags['hybrid']['status']=='detected').sum()}", flush=True)

    # backtest
    ret_oos = r.reindex(oos_wk.index)
    strat = {
        "WK x Trend": backtest(hyb, ret_oos),
        "WK-means": backtest(oos_wk["regime"], ret_oos),
        "HMM": backtest(oos_hmm["regime"], ret_oos),
    }
    perf = pd.DataFrame({nm: perf_stats(bt["ret"], bt["pos"])
                         for nm, bt in strat.items()})
    bh = (1 + ret_oos).cumprod()
    perf["Buy&Hold"] = {
        "CAGR": bh.iloc[-1] ** (252 / len(bh)) - 1,
        "AnnVol": ret_oos.std() * np.sqrt(252),
        "Sharpe": ret_oos.mean() * 252 / (ret_oos.std() * np.sqrt(252)),
        "MaxDD": (bh / bh.cummax() - 1).min(),
        "Calmar": np.nan, "Switches/yr": 0.0,
    }
    perf = perf[["WK x Trend", "WK-means", "HMM", "Buy&Hold"]]
    perf.to_csv(os.path.join(outdir, "perf_summary.csv"))
    print(perf.round(3).to_string())

    # ---- fig2: regimes + probabilities
    fig, axes = plt.subplots(3, 1, figsize=(12.5, 9.5), sharex=True,
                             gridspec_kw={"height_ratios": [2.2, 1.6, 0.5]})
    ax = axes[0]
    ax.plot(oos_wk.index, close.reindex(oos_wk.index), color="black", lw=0.8)
    regime_bands(ax, hyb)
    ax.set_yscale("log")
    plain_log_axis(ax)
    ev = [(pd.Timestamp(d), s) for d, s in KNOWN_EVENTS
          if oos_wk.index[0] <= pd.Timestamp(d) <= oos_wk.index[-1]]
    for d, s in ev:
        ax.axvline(d, color="grey", lw=0.6, ls=":", alpha=0.7)
    ax.set_title(f"{tkr}: WK-means x Trend hybrid - out-of-sample regimes "
                 f"{oos_wk.index[0].year}-{oos_wk.index[-1].year} "
                 "(frozen SPY params)", fontsize=10)
    ax = axes[1]
    cols = prob_cols(oos_wk)
    ax.stackplot(oos_wk.index, *[oos_wk[c].values for c in cols], labels=cols,
                 colors=[COLORS[c] for c in cols], alpha=0.75)
    ax.set_ylim(0, 1)
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.02), ncol=3, frameon=False)
    ax.set_title("WK-means filtered regime probabilities", fontsize=9)
    ax = axes[2]
    regime_bands(ax, oos_wk["regime"])
    ax.set_yticks([])
    ax.set_title("Pure WK-means distribution state", fontsize=9)
    fig.savefig(os.path.join(outdir, "fig2_walkforward.png"))
    plt.close(fig)

    # ---- fig3: backtest
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.2, 0.6]})
    ax = axes[0]
    for nm, bt in strat.items():
        ax.plot(bt.index, bt["equity"], lw=1.1,
                label=f"{nm} (Sharpe {perf.loc['Sharpe', nm]:.2f})")
    ax.plot(bh.index, bh, lw=1.1, color="black", alpha=0.6,
            label=f"Buy & Hold (Sharpe {perf.loc['Sharpe', 'Buy&Hold']:.2f})")
    ax.set_yscale("log")
    plain_log_axis(ax)
    ax.legend(loc="upper left", frameon=False)
    ax.set_title(f"{tkr} regime strategy equity (OOS {oos_wk.index[0].year}-"
                 f"{oos_wk.index[-1].year}, 1-day lag, 2bp cost)", fontsize=10)
    ax = axes[1]
    dd = strat["WK x Trend"]["equity"] / strat["WK x Trend"]["equity"].cummax() - 1
    ddb = bh / bh.cummax() - 1
    ax.fill_between(dd.index, dd, 0, color=COLORS["Bear"], alpha=0.4,
                    label="WK x Trend strat")
    ax.plot(ddb.index, ddb, color="black", lw=0.7, alpha=0.7, label="Buy & Hold")
    ax.legend(loc="lower left", frameon=False)
    ax.set_title("Drawdown", fontsize=9)
    ax = axes[2]
    regime_bands(ax, hyb)
    ax.set_yticks([])
    ax.set_title("WK x Trend regime", fontsize=9)
    fig.savefig(os.path.join(outdir, "fig3_backtest.png"))
    plt.close(fig)

    # ---- fig5: current status
    zoom = oos_wk.iloc[-500:]
    fig, axes = plt.subplots(2, 1, figsize=(12, 6.5), sharex=True)
    ax = axes[0]
    ax.plot(zoom.index, close.reindex(zoom.index), color="black", lw=1.0)
    regime_bands(ax, hyb.reindex(zoom.index))
    for _, t in trans_hyb[trans_hyb["date"] >= zoom.index[0]].iterrows():
        ax.axvline(t["date"], color="blue", lw=0.8, ls="--", alpha=0.6)
    last = oos_wk.iloc[-1]
    txt = (f"{tkr} current (hybrid): {hyb.iloc[-1]} | dist: {last['regime']}\n"
           f"P(Bull)={last['Bull']:.2f}  P(Choppy)={last['Choppy']:.2f}  "
           f"P(Bear)={last['Bear']:.2f}  as of {fmt_date(oos_wk.index[-1])}")
    ax.text(0.99, 0.04, txt, transform=ax.transAxes, fontsize=10, va="bottom",
            ha="right", bbox=dict(facecolor="white", alpha=0.85, edgecolor="grey"))
    ax.set_title(f"{tkr} recent 500 trading days", fontsize=10)
    ax = axes[1]
    for c in cols:
        ax.plot(zoom.index, zoom[c], color=COLORS[c], lw=1.1, label=c)
    ax.legend(loc="upper left", ncol=3, frameon=False)
    ax.set_ylim(0, 1)
    fig.savefig(os.path.join(outdir, "fig5_current_status.png"))
    plt.close(fig)

    # ---- parameter-transfer check (CSV only)
    rows = []
    for mom in (63, 126, 189, 252):
        for band in (0.0, 0.01, 0.02, 0.03, 0.05):
            h = hybrid_regime(oos_wk, close, mom=mom, band=band)
            bt = backtest(h, ret_oos)
            st = perf_stats(bt["ret"], bt["pos"])
            lg = detection_lags(h, episodes)
            rows.append({"mom": mom, "band": band,
                         "detected": int((lg["status"] == "detected").sum()),
                         **st})
    pd.DataFrame(rows).to_csv(os.path.join(outdir, "trend_grid_transfer.csv"),
                              index=False)

    # current-status info
    chg = hyb.ne(hyb.shift())
    last_change = hyb.index[chg][-1] if chg.any() else hyb.index[0]
    lg_h = lags["hybrid"]
    summary = {
        "ticker": tkr,
        "data": f"{close.index[0].date()} -> {close.index[-1].date()}",
        "oos_start": fmt_date(oos_wk.index[0]),
        "current_regime": hyb.iloc[-1],
        "current_dist_state": last["regime"],
        "probabilities": {c: round(float(last[c]), 3) for c in cols},
        "days_in_regime": int((oos_wk.index[-1] - last_change).days),
        "regime_share": {nm: round(float((hyb == nm).mean()), 3)
                         for nm in ("Bull", "Choppy", "Bear")},
        "recent_transitions": [
            {"date": fmt_date(t["date"]), "from": t["from"], "to": t["to"]}
            for _, t in trans_hyb.tail(4).iterrows()],
        "detection": {
            "episodes": len(episodes),
            "detected": int((lg_h["status"] == "detected").sum()),
            "median_lag": None if lg_h["lag_days_vs_-10%"].isna().all()
            else float(lg_h["lag_days_vs_-10%"].median()),
        },
        "perf": {nm: {k: round(float(v), 3) for k, v in perf[nm].items()}
                 for nm in perf.columns},
    }
    print(f"[{tkr}] done in {time.time()-t0:.0f}s", flush=True)
    return summary


def main():
    results = {}
    for tkr in TICKERS:
        results[tkr] = run_ticker(tkr)

    # ---- fig6: cross-asset comparison
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    x = np.arange(len(TICKERS))
    w_ = 0.2
    strategies = ["WK x Trend", "WK-means", "HMM", "Buy&Hold"]
    shades = ["#1b5e20", "#4caf50", "#a5d6a7", "#757575"]
    for i, (s, c) in enumerate(zip(strategies, shades)):
        v = [results[t]["perf"][s]["Sharpe"] for t in TICKERS]
        axes[0].bar(x + (i - 1.5) * w_, v, w_, label=s, color=c)
        v2 = [results[t]["perf"][s]["MaxDD"] for t in TICKERS]
        axes[1].bar(x + (i - 1.5) * w_, v2, w_, label=s, color=c)
    for ax, ttl in [(axes[0], "OOS Sharpe"), (axes[1], "OOS Max drawdown")]:
        ax.set_xticks(x, TICKERS)
        ax.set_title(f"{ttl} (frozen SPY params, ticker-specific OOS windows)",
                     fontsize=10)
        ax.grid(alpha=0.25)
    axes[0].legend(frameon=False, fontsize=8)
    txt = "   |   ".join(
        f"{t}: {results[t]['current_regime']}"
        f" (P_bear={results[t]['probabilities']['Bear']:.2f})"
        for t in TICKERS)
    fig.suptitle(f"Current regime as of {results['SPY']['data'].split(' -> ')[1]}: "
                 f"{txt}", fontsize=10)
    fig.savefig(os.path.join(OUTM, "fig6_cross_asset.png"))
    plt.close(fig)

    with open(os.path.join(OUTM, "summary_multi.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\n=== CROSS-ASSET SUMMARY ===")
    print(json.dumps({t: {k: results[t][k] for k in
                          ("current_regime", "probabilities", "days_in_regime",
                           "detection")} for t in TICKERS}, indent=2))


if __name__ == "__main__":
    main()
