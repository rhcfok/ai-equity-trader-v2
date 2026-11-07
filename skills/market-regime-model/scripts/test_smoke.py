"""Smoke tests: correctness checks on synthetic regime data + pipeline pieces."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from wkmeans import wkmeans, soft_assign, semantic_labels, wasserstein_p_matrix
from baselines import MKMeans, HMMRegime
from pipeline import make_windows, apply_hysteresis, forward_filter, transition_events
from backtest import backtest, perf_stats, drawdown_episodes

rng = np.random.default_rng(7)

# ---- 1. synthetic 2-regime data: calm drift + violent bear -------------
n1, n2, n3 = 500, 250, 500
r = np.concatenate([
    rng.normal(0.0004, 0.008, n1),   # bull
    rng.normal(-0.002, 0.030, n2),   # bear (fat tail via big vol)
    rng.normal(0.0003, 0.006, n3),   # bull again
])
true_seg = np.array([0]*n1 + [1]*n2 + [0]*n3)

W = 40
X, day_idx = make_windows(r, W)
# true label of a window = majority of its members
true_win = np.array([np.bincount(true_seg[i-W+1:i+1]).argmax() for i in day_idx])

lab, C, inertia = wkmeans(X, 2, p=2.0, n_init=4, seed=1)
names, stats = semantic_labels(C)
acc = max(
    np.mean([names[l] == ("Bull" if t == 0 else "Bear") for l, t in zip(lab, true_win)]),
    np.mean([names[l] == ("Bear" if t == 0 else "Bull") for l, t in zip(lab, true_win)]),
)
print(f"[1] synthetic WK-means accuracy: {acc:.3f}  (expect ~1.0 away from boundaries)")
print(f"    cluster stats: {stats}")

# transition day detection: first window labeled Bear
first_bear = day_idx[np.array([names[l] for l in lab]).tolist().index("Bear")]
print(f"    bear detected at day {first_bear} (true start {n1}, window end >= {n1+W-1} possible) "
      f"-> lag {first_bear - (n1+W-1)} days from earliest observable")

# ---- 2. distance & barycenter sanity ------------------------------------
a = np.sort(rng.normal(0, 0.01, 40)); b = np.sort(rng.normal(0.001, 0.02, 40))
D = wasserstein_p_matrix(a[None], np.vstack([a, b]), p=2.0)[0]
assert D[0] < D[1] + 1e-12, "self-distance should be ~0"
# W2 between identical samples = 0
assert abs(D[0]) < 1e-12
print("[2] W-distance sanity OK")

# ---- 3. hysteresis behaviour --------------------------------------------
P = np.array([[0.9, 0.05, 0.05]] * 5 + [[0.35, 0.60, 0.05]] * 2 +
             [[0.9, 0.05, 0.05]] + [[0.35, 0.60, 0.05]] * 3 + [[0.9, 0.05, 0.05]] * 2)
labs = apply_hysteresis(P, theta=0.55, m_conf=3, escape=0.85)
# 2-day blip should NOT flip; 3-day run should flip; a 0.9-prob day
# triggers the crash-escape immediate switch back
assert labs[6] == 0 and labs[10] == 1 and labs[11] == 0, labs
print("[3] hysteresis rejects 2-day whipsaw, accepts 3-day switch, "
      "escape-hatch fires at p>=0.85 OK")

# ---- 4. forward filter matches HMM likelihood ---------------------------
rr = rng.normal(0, 0.01, 400)
hmm = HMMRegime(2, n_starts=1, n_iter=50).fit(rr)
m = hmm.model
Pf = forward_filter(rr, m.transmat_, m.means_.ravel(), m.covars_.ravel(),
                    m.startprob_)
Ps = m.predict_proba(rr.reshape(-1, 1))  # smoothed; last row == filtered last row
assert np.allclose(Pf[-1], Ps[-1], atol=1e-6), (Pf[-1], Ps[-1])
print("[4] forward filter == hmmlearn filtered posterior (last row) OK")

# ---- 5. backtest & events ------------------------------------------------
idx = pd.bdate_range("2020-01-01", periods=300)
px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 300))), index=idx)
reg = pd.Series(["Bull"]*100 + ["Bear"]*100 + ["Bull"]*100, index=idx)
rets = np.log(px).diff().dropna()
reg = reg.reindex(rets.index)  # align regime labels to return dates
bt = backtest(reg, rets)
st = perf_stats(bt["ret"], bt["pos"])
assert st["Sharpe"] == st["Sharpe"] and len(bt) == len(rets)
te = transition_events(reg)
assert len(te) == 2 and te.iloc[0]["to"] == "Bear"
ep = drawdown_episodes(px, threshold=-0.15, early=-0.10)
print(f"[5] backtest/events OK  Sharpe={st['Sharpe']:.2f}  episodes={len(ep)}")

print("\nALL SMOKE TESTS PASSED")
