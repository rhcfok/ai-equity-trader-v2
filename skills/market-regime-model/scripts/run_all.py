"""
run_all.py - end-to-end pipeline for Wasserstein-k-means market regime model.

Steps
  1. Load SPY daily data (1993 -> present, cached).
  2. In-sample regime maps: WK-means vs MK-means vs HMM (replica of the
     paper's Fig. 2 comparison, extended to 2026 on daily data).
  3. MMD cluster self-similarity (paper's cluster-quality score).
  4. Walk-forward out-of-sample regime detection (2005 -> present) for the
     three models, with hysteresis transition detection.
  5. Transition event tables + detection lag vs ex-post drawdown episodes.
  6. Regime-strategy backtest vs buy & hold.
  7. Sensitivity: window length w, cluster count k, hysteresis (theta, m).
  8. Current status dashboard (as of the latest close).

Outputs: out/*.png, out/*.csv, out/summary.json
"""

from __future__ import annotations

import json
import os
import sys
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wkmeans import wkmeans, semantic_labels, cluster_self_similarity
from baselines import MKMeans, HMMRegime
from pipeline import (load_spy, log_returns, make_windows, walk_forward_windows,
                      walk_forward_hmm, transition_events, apply_hysteresis,
                      hybrid_regime, KNOWN_EVENTS)
from backtest import backtest, perf_stats, drawdown_episodes, detection_lags

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)

COLORS = {"Bull": "#2e7d32", "Choppy": "#f0ad00", "Bear": "#c62828"}
CANON = ["Bull", "Choppy", "Bear"]

plt.rcParams.update({
    "figure.dpi": 110, "savefig.dpi": 130, "font.size": 9,
    "axes.grid": True, "grid.alpha": 0.25, "axes.spines.top": False,
    "axes.spines.right": False, "figure.autolayout": True,
})


def prob_cols(df: pd.DataFrame):
    return [c for c in df.columns if c not in ("regime", "fitted")]


def regime_bands(ax, regime: pd.Series, ymax=1.0, alpha=0.30):
    """Colored background bands for a categorical regime series."""
    items = list(regime.items())
    start, prev = regime.index[0], regime.iloc[0]
    for d, v in items[1:]:
        if v != prev:
            ax.axvspan(start, d, color=COLORS.get(prev, "grey"),
                       alpha=alpha, lw=0)
            start, prev = d, v
    ax.axvspan(start, regime.index[-1], color=COLORS.get(prev, "grey"),
               alpha=alpha, lw=0)


def fmt_date(d):
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def plain_log_axis(ax):
    """Show plain numbers instead of 8 x 10^1 offset notation on log axes."""
    from matplotlib.ticker import FuncFormatter, NullFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, p: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def main():
    t0 = time.time()
    print("[1/8] Loading SPY ...")
    close = load_spy(cache_path=os.path.join(OUT, "spy_daily.csv"))["Close"]
    r = log_returns(close)
    print(f"      {close.index[0].date()} -> {close.index[-1].date()}, "
          f"{len(close)} days")

    # ------------------------------------------------------------------ 2
    print("[2/8] In-sample fits (w=40, k=3) ...")
    W, K = 40, 3
    X, day_idx = make_windows(r.values, W)
    dates_win = r.index[day_idx]

    lab_wk, C_wk, _ = wkmeans(X, K, p=2.0, n_init=8, seed=42)
    names_wk, stats_wk = semantic_labels(C_wk)

    mk = MKMeans(K).fit(X)
    names_mk, _ = semantic_labels(mk.centroid_stats)

    hmm_is = HMMRegime(K, n_starts=1, n_iter=100).fit(r.values)
    P_is = hmm_is.soft_assign(r.values)

    regime_wk_is = pd.Series([names_wk[l] for l in lab_wk], index=dates_win)
    regime_mk_is = pd.Series([names_mk[l] for l in mk.km.labels_], index=dates_win)
    regime_hmm_is = pd.Series([CANON[j] for j in P_is.argmax(1)], index=r.index)

    # ------------------------------------------------------------------ 3
    print("[3/8] MMD self-similarity (cluster homogeneity) ...")
    sigma = float(np.median(X.std(axis=1)))
    sim_wk = cluster_self_similarity(X, lab_wk, sigma=sigma, max_pairs=60)
    sim_mk = cluster_self_similarity(X, mk.km.labels_, sigma=sigma, max_pairs=60)

    # Fig 1: in-sample regime map (paper Fig. 2 replica, extended to 2026)
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    for ax, reg, title in zip(
        axes,
        [regime_wk_is, regime_mk_is, regime_hmm_is],
        [f"WK-means (Wasserstein, this model)",
         "MK-means (moments + Euclidean, paper baseline)",
         "Gaussian HMM (Hamilton-style baseline)"],
    ):
        ax.plot(reg.index, close.reindex(reg.index), color="black", lw=0.7)
        regime_bands(ax, reg)
        ax.set_yscale("log")
        plain_log_axis(ax)
        ax.set_title(title + "  |  in-sample 1993-2026, SPY (log scale)",
                     fontsize=10)
    axes[0].scatter([], [], color=COLORS["Bull"], label="Bull")
    axes[0].scatter([], [], color=COLORS["Choppy"], label="Choppy")
    axes[0].scatter([], [], color=COLORS["Bear"], label="Bear")
    axes[0].legend(loc="upper left", ncol=3, frameon=False)
    fig.savefig(os.path.join(OUT, "fig1_regime_map_insample.png"))
    plt.close(fig)

    # ------------------------------------------------------------------ 4
    print("[4/8] Walk-forward out-of-sample detection (2005 -> now) ...")
    oos_wk = walk_forward_windows(r, w=W, k=K, theta=0.55, m_conf=3)
    print(f"      WK done {time.time()-t0:.0f}s")
    oos_mk = walk_forward_windows(r, w=W, k=K, theta=0.55, m_conf=3,
                                  kind="mkmeans")
    print(f"      MK done {time.time()-t0:.0f}s")
    oos_hmm = walk_forward_hmm(r, k=K, theta=0.55, m_conf=3)
    print(f"      HMM done {time.time()-t0:.0f}s")

    oos_wk.to_csv(os.path.join(OUT, "regime_wk_oos.csv"))
    oos_hmm.to_csv(os.path.join(OUT, "regime_hmm_oos.csv"))

    trans_wk = transition_events(oos_wk["regime"])
    trans_hmm = transition_events(oos_hmm["regime"])
    trans_wk.to_csv(os.path.join(OUT, "transitions_wk.csv"), index=False)
    print(f"      WK switches: {len(trans_wk)}, HMM switches: {len(trans_hmm)}")

    # headline model: WK-means distribution state x trend state
    hyb = hybrid_regime(oos_wk, close)
    hyb.to_csv(os.path.join(OUT, "regime_hybrid_oos.csv"))
    trans_hyb = transition_events(hyb)
    trans_hyb.to_csv(os.path.join(OUT, "transitions_hybrid.csv"), index=False)
    print(f"      Hybrid switches: {len(trans_hyb)}")

    # ------------------------------------------------------------------ 5
    print("[5/8] Transition quality vs ex-post drawdown episodes ...")
    episodes = drawdown_episodes(close, threshold=-0.15, early=-0.10)
    episodes = episodes[episodes["trough_date"] >= oos_wk.index[0]]
    lags_wk = detection_lags(oos_wk["regime"], episodes)
    lags_hmm = detection_lags(oos_hmm["regime"], episodes)
    lags_hyb = detection_lags(hyb, episodes)
    lags_wk.to_csv(os.path.join(OUT, "detection_lags_wk.csv"), index=False)
    lags_hmm.to_csv(os.path.join(OUT, "detection_lags_hmm.csv"), index=False)
    lags_hyb.to_csv(os.path.join(OUT, "detection_lags_hybrid.csv"), index=False)
    print(lags_hyb.to_string())

    # ------------------------------------------------------------------ 6
    print("[6/8] Backtest ...")
    ret_oos = r.reindex(oos_wk.index)
    strat = {
        "WK x Trend": backtest(hyb, ret_oos),
        "WK-means": backtest(oos_wk["regime"], ret_oos),
        "MK-means": backtest(oos_mk["regime"], ret_oos),
        "HMM": backtest(oos_hmm["regime"], ret_oos),
    }
    perf = pd.DataFrame(
        {nm: perf_stats(bt["ret"], bt["pos"]) for nm, bt in strat.items()}
    )
    bh = (1 + ret_oos).cumprod()
    perf["Buy&Hold"] = {
        "CAGR": bh.iloc[-1] ** (252 / len(bh)) - 1,
        "AnnVol": ret_oos.std() * np.sqrt(252),
        "Sharpe": ret_oos.mean() * 252 / (ret_oos.std() * np.sqrt(252)),
        "MaxDD": (bh / bh.cummax() - 1).min(),
        "Calmar": np.nan, "Switches/yr": 0.0,
    }
    perf = perf[["WK x Trend", "WK-means", "MK-means", "HMM", "Buy&Hold"]]
    perf.to_csv(os.path.join(OUT, "perf_summary.csv"))
    print(perf.round(3).to_string())

    # Fig 3: backtest
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
    ax.set_title("Regime strategy equity (out-of-sample, 1-day lag, 2bp cost) — "
                 "Bull=100% long, Choppy=25%, Bear=cash", fontsize=10)
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
    ax.set_title("WK-means regime", fontsize=9)
    fig.savefig(os.path.join(OUT, "fig3_backtest.png"))
    plt.close(fig)

    # ------------------------------------------------------------------ 7
    print("[7/8] Sensitivity ...")
    # 7a: hysteresis grid (theta x m) on the saved WK probability path
    P = oos_wk[prob_cols(oos_wk)].values
    rows = []
    for theta in (0.45, 0.55, 0.65):
        for m in (1, 3, 5):
            labs = apply_hysteresis(P, theta=theta, m_conf=m)
            reg = pd.Series([CANON[j] for j in labs], index=oos_wk.index)
            bt = backtest(reg, ret_oos)
            st = perf_stats(bt["ret"], bt["pos"])
            rows.append({"theta": theta, "m_conf": m, **st})
    sens = pd.DataFrame(rows)
    sens.to_csv(os.path.join(OUT, "sensitivity_theta_m.csv"), index=False)

    # 7b: window length and k variants (walk-forward reruns)
    variants = []
    for w_, k_ in [(20, 3), (60, 3), (40, 2)]:
        oos_v = walk_forward_windows(r, w=w_, k=k_, theta=0.55, m_conf=3)
        pos_v = {"Bull": 1.0, "Choppy": 0.25, "Bear": 0.0} if k_ == 3 else \
            {"Bull": 1.0, "Bear": 0.0}
        bt = backtest(oos_v["regime"], ret_oos.reindex(oos_v.index),
                      positions=pos_v)
        st = perf_stats(bt["ret"], bt["pos"])
        variants.append({"model": f"WK w={w_}, k={k_}", **st})
        print(f"      variant w={w_} k={k_} done {time.time()-t0:.0f}s")
    pd.DataFrame(variants).to_csv(os.path.join(OUT, "sensitivity_w_k.csv"),
                                  index=False)

    # 7c: hybrid trend-parameter sensitivity (post-processing, cheap)
    hyb_rows = []
    for mom, band in [(63, 0.02), (126, 0.01), (126, 0.02), (126, 0.05),
                      (189, 0.02), (252, 0.02)]:
        h = hybrid_regime(oos_wk, close, mom=mom, band=band)
        bt = backtest(h, ret_oos)
        st = perf_stats(bt["ret"], bt["pos"])
        lg = detection_lags(h, episodes)
        hyb_rows.append({
            "mom_window": mom, "band": band,
            "detected": int((lg["status"] == "detected").sum()),
            "median_lag": float(lg["lag_days_vs_-10%"].median()),
            **st,
        })
    pd.DataFrame(hyb_rows).to_csv(os.path.join(OUT, "sensitivity_hybrid_trend.csv"),
                                  index=False)
    print(pd.DataFrame(hyb_rows).round(3).to_string())

    # Fig 4: sensitivity heatmaps
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    for ax, val, fmtv in [(axes[0], "Sharpe", "%.2f"),
                          (axes[1], "Switches/yr", "%.1f")]:
        piv = sens.pivot(index="theta", columns="m_conf", values=val)
        im = ax.imshow(piv.values, cmap="RdYlGn" if val == "Sharpe" else "OrRd_r",
                       aspect="auto")
        ax.set_xticks(range(len(piv.columns)), [f"m={m}" for m in piv.columns])
        ax.set_yticks(range(len(piv.index)), [f"θ={t:.2f}" for t in piv.index])
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                ax.text(j, i, fmtv % piv.values[i, j], ha="center",
                        va="center", fontsize=9)
        ax.set_title(f"WK-means walk-forward {val}  (w=40, k=3)", fontsize=10)
        ax.grid(False)
    fig.savefig(os.path.join(OUT, "fig4_sensitivity.png"))
    plt.close(fig)

    # ------------------------------------------------------------------ 8
    print("[8/8] Current status + fig2/fig5 ...")
    last = oos_wk.iloc[-1]
    recent_trans = trans_hyb.tail(6)

    chg = hyb.ne(hyb.shift())
    last_change = hyb.index[chg][-1] if chg.any() else hyb.index[0]
    status = {
        "as_of": fmt_date(oos_wk.index[-1]),
        "regime": hyb.iloc[-1],
        "dist_regime": last["regime"],
        "probabilities": {c: round(float(last[c]), 3) for c in prob_cols(oos_wk)},
        "days_in_regime": int((oos_wk.index[-1] - last_change).days),
        "recent_transitions": [
            {"date": fmt_date(x.date), "from": x["from"], "to": x["to"]}
            for _, x in recent_trans.iterrows()
        ],
        "cluster_stats": stats_wk,
        "mmd_self_similarity": {"WK": sim_wk, "MK": sim_mk,
                                "sigma": round(sigma, 5)},
        "detection": {
            "episodes_total": len(episodes),
            "hybrid_detected": int((lags_hyb["status"] == "detected").sum()),
            "hybrid_median_lag_days": float(lags_hyb["lag_days_vs_-10%"].median()),
            "wk_detected": int((lags_wk["status"] == "detected").sum()),
            "wk_median_lag_days": float(lags_wk["lag_days_vs_-10%"].median()),
            "hmm_detected": int((lags_hmm["status"] == "detected").sum()),
            "hmm_median_lag_days": float(lags_hmm["lag_days_vs_-10%"].median()),
        },
    }

    # Fig 2: walk-forward regime + probabilities + event annotations
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
    ax.set_title("WK-means x Trend hybrid - out-of-sample regimes 2005-2026 "
                 "(dotted lines = known market events)", fontsize=10)
    ax = axes[1]
    cols = prob_cols(oos_wk)
    ax.stackplot(oos_wk.index, *[oos_wk[c].values for c in cols],
                 labels=cols, colors=[COLORS[c] for c in cols], alpha=0.75)
    ax.set_ylim(0, 1)
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.02), ncol=3,
              frameon=False)
    ax.set_title("WK-means filtered regime probabilities (distribution state)",
                 fontsize=9)
    ax = axes[2]
    regime_bands(ax, oos_wk["regime"])
    ax.set_yticks([])
    ax.set_title("Pure WK-means distribution state (for comparison)", fontsize=9)
    fig.savefig(os.path.join(OUT, "fig2_walkforward.png"))
    plt.close(fig)

    # Fig 5: current status zoom (last 500 trading days)
    zoom = oos_wk.iloc[-500:]
    zc = close.reindex(zoom.index)
    fig, axes = plt.subplots(2, 1, figsize=(12, 6.5), sharex=True)
    ax = axes[0]
    ax.plot(zoom.index, zc, color="black", lw=1.0)
    regime_bands(ax, hyb.reindex(zoom.index))
    for _, t in trans_hyb[trans_hyb["date"] >= zoom.index[0]].iterrows():
        ax.axvline(t["date"], color="blue", lw=0.8, ls="--", alpha=0.6)
    txt = (f"Current (hybrid): {hyb.iloc[-1]} | distribution state: "
           f"{last['regime']}\nP(Bull)={last['Bull']:.2f}  "
           f"P(Choppy)={last['Choppy']:.2f}  P(Bear)={last['Bear']:.2f}  "
           f"as of {fmt_date(oos_wk.index[-1])}")
    ax.text(0.99, 0.04, txt, transform=ax.transAxes, fontsize=10,
            va="bottom", ha="right", bbox=dict(facecolor="white", alpha=0.85,
                                   edgecolor="grey"))
    ax.set_title("Recent 500 trading days - WK-means x Trend regime & "
                 "transitions (dashed blue = detected transition)", fontsize=10)
    ax = axes[1]
    for c in cols:
        ax.plot(zoom.index, zoom[c], color=COLORS[c], lw=1.1, label=c)
    ax.legend(loc="upper left", ncol=3, frameon=False)
    ax.set_ylim(0, 1)
    fig.savefig(os.path.join(OUT, "fig5_current_status.png"))
    plt.close(fig)

    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(status, f, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(status, indent=2, default=str))
    print(f"\nDone in {time.time()-t0:.0f}s. Outputs in {OUT}")


if __name__ == "__main__":
    main()
