"""
Baseline regime models compared against WK-means:

1. MK-means (paper's benchmark): standard k-means on window moments
   (mean, std, skew, kurt), standardized per coordinate, Euclidean metric.
2. Gaussian HMM (Hamilton 1989 style): 3-state HMM on daily log-returns,
   fitted with EM (hmmlearn), soft posteriors natively available.

Both are wrapped to expose the same interface as wkmeans:
  fit -> centroids/state stats, and a soft-assignment posterior function.
"""

from __future__ import annotations

import numpy as np
from scipy import stats as sps
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

try:  # hmmlearn is optional at import time
    from hmmlearn.hmm import GaussianHMM
except Exception:  # pragma: no cover
    GaussianHMM = None

from wkmeans import semantic_labels


# ---------------------------------------------------------------------------
# MK-means: moment features + Euclidean k-means
# ---------------------------------------------------------------------------

def window_moments(R: np.ndarray) -> np.ndarray:
    """First four raw moments per window. R is (M, N) of returns."""
    mean = R.mean(axis=1)
    std = R.std(axis=1, ddof=1)
    skew = sps.skew(R, axis=1)
    kurt = sps.kurtosis(R, axis=1)
    return np.column_stack([mean, std, skew, kurt])


class MKMeans:
    """Moment k-means baseline. Soft assignment via softmax on Euclidean
    distances to keep it comparable with the WK-means posterior."""

    def __init__(self, k: int = 3, seed: int = 42):
        self.k = k
        self.seed = seed
        self.scaler = StandardScaler()
        self.km = None
        self.centroid_stats = None
        self.names_ = None

    def fit(self, R: np.ndarray):
        F = self.scaler.fit_transform(window_moments(R))
        self.km = KMeans(n_clusters=self.k, n_init=10, random_state=self.seed).fit(F)
        # cluster stats computed on *untransformed* windows for semantics
        self.centroid_stats = np.vstack(
            [R[self.km.labels_ == j].mean(axis=0) for j in range(self.k)]
        )
        self.names_, _ = semantic_labels(self.centroid_stats)
        return self

    def soft_assign(self, R: np.ndarray) -> np.ndarray:
        F = self.scaler.transform(window_moments(R))
        d2 = ((F[:, None, :] - self.km.cluster_centers_[None, :, :]) ** 2).sum(axis=2)
        temp = max(np.median(d2), 1e-12)
        z = -d2 / temp
        z -= z.max(axis=1, keepdims=True)
        w = np.exp(z)
        return w / w.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Gaussian HMM on daily returns
# ---------------------------------------------------------------------------

class HMMRegime:
    """3-state Gaussian HMM on daily log-returns.

    State semantics re-derived after every fit by ranking state means
    (top = Bull, bottom = Bear, middle = Choppy) to handle label switching.
    """

    def __init__(self, k: int = 3, seed: int = 42, n_iter: int = 200, n_starts: int = 3):
        self.k = k
        self.seed = seed
        self.n_iter = n_iter
        self.n_starts = n_starts
        self.model = None
        self.names_ = None
        self.state_stats_ = None

    def fit(self, r_col: np.ndarray):
        """r_col: 1-D array of daily log returns used for EM."""
        if GaussianHMM is None:
            raise ImportError("hmmlearn is required for the HMM baseline")
        X = np.asarray(r_col).reshape(-1, 1)
        best, best_ll = None, -np.inf
        rng = np.random.default_rng(self.seed)
        for attempt in range(self.n_starts):  # multi-start EM to avoid local optima
            m = GaussianHMM(
                n_components=self.k,
                covariance_type="full",
                n_iter=self.n_iter,
                tol=1e-4,
                random_state=int(rng.integers(0, 1e6)),
                init_params="stmc",
            )
            m.fit(X)
            ll = m.score(X)
            if ll > best_ll:
                best, best_ll = m, ll
        self.model = best
        means = self.model.means_.ravel()
        order = np.argsort(means)
        self.names_ = {
            int(order[0]): "Bear",
            int(order[-1]): "Bull",
        }
        for j in range(self.k):
            self.names_.setdefault(int(j), "Choppy")
        self.state_stats_ = {
            j: {"ann_ret": float(means[j] * 252),
                "ann_vol": float(np.sqrt(self.model.covars_[j].ravel()[0] * 252))}
            for j in range(self.k)
        }
        return self

    def soft_assign(self, r_col: np.ndarray) -> np.ndarray:
        """Posterior state probabilities; columns re-ordered so that column
        order is always [Bull, Choppy, Bear] regardless of internal labels."""
        X = np.asarray(r_col).reshape(-1, 1)
        P = self.model.predict_proba(X)
        # map to canonical column order: Bull, Choppy, Bear
        cols = {}
        for j, nm in self.names_.items():
            cols.setdefault(nm, []).append(j)
        bull = cols.get("Bull")[0]
        bear = cols.get("Bear")[0]
        chop = [j for nm, js in cols.items() if nm == "Choppy" for j in js]
        if self.k == 2:
            return np.column_stack([P[:, bull], P[:, bear]])
        if len(chop) > 1:  # tie-break by middle mean
            chop = [max(chop, key=lambda j: self.model.means_.ravel()[j])]
        return np.column_stack([P[:, bull], P[:, chop[0]], P[:, bear]])
