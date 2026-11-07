"""
Wasserstein k-means (WK-means) for market regime clustering.

Implements the method of Horvath, Issa & Muguruza (2021),
"Clustering Market Regimes using the Wasserstein Distance" (arXiv:2110.11848).

Key objects
-----------
- A rolling window of N log-returns is represented by its *empirical measure*:
  the vector of sorted returns (order statistics), each atom with weight 1/N.
- p-Wasserstein distance between two such windows (Eq. 21 of the paper):
      Wp(mu, nu)^p = (1/N) * sum_i |alpha_i - beta_i|^p
  where alpha, beta are the sorted return vectors. (For 1-D empirical measures
  with equal atom counts the optimal transport map is monotone, so this is exact.)
- Wasserstein barycenter of a cluster (Eq. 22 for p=1): elementwise median
  (p=1) or elementwise mean (p=2) of the sorted vectors in the cluster.

This module is metric-only: it never assumes a parametric return distribution,
which is exactly the property the paper argues gives robustness to fat tails,
skewness and volatility clustering.
"""

from __future__ import annotations

import numpy as np

REGIME_NAMES = {0: "Bull", 1: "Choppy", 2: "Bear"}


# ---------------------------------------------------------------------------
# Distances and barycenters on sorted return vectors
# ---------------------------------------------------------------------------

def wasserstein_p_matrix(X: np.ndarray, C: np.ndarray, p: float = 2.0) -> np.ndarray:
    """Pairwise p-Wasserstein distance matrix between windows X (M x N) and
    centroids C (K x N). Both must have rows sorted ascending and equal atom
    count N. Returns M x K matrix of W_p distances (not raised to power 1/p
    until the end: we return (mean |x-c|^p)^(1/p))."""
    # (M, K, N) broadcast
    diff = X[:, None, :] - C[None, :, :]
    return np.mean(np.abs(diff) ** p, axis=2) ** (1.0 / p)


def wasserstein_barycenter(cluster_windows: np.ndarray, p: float = 2.0) -> np.ndarray:
    """Barycenter of a set of equal-atom-count empirical measures on R.

    p=1 -> elementwise median (Eq. 22 of the paper)
    p=2 -> elementwise mean   (standard W2 barycenter in 1-D)
    """
    if p == 1:
        return np.median(cluster_windows, axis=0)
    return np.mean(cluster_windows, axis=0)


# ---------------------------------------------------------------------------
# WK-means clustering
# ---------------------------------------------------------------------------

def _kmeanspp_init(X: np.ndarray, k: int, p: float, rng: np.random.Generator) -> np.ndarray:
    """k-means++ seeding using the Wasserstein metric."""
    n = X.shape[0]
    centroids = [X[rng.integers(n)]]
    for _ in range(1, k):
        d = wasserstein_p_matrix(X, np.asarray(centroids), p).min(axis=1)
        d2 = d ** 2
        probs = d2 / d2.sum() if d2.sum() > 0 else np.full(n, 1.0 / n)
        centroids.append(X[rng.choice(n, p=probs)])
    return np.asarray(centroids)


def wkmeans(
    X: np.ndarray,
    k: int,
    p: float = 2.0,
    n_init: int = 10,
    max_iter: int = 200,
    tol: float = 1e-8,
    seed: int = 42,
    init: np.ndarray | None = None,
):
    """Cluster sorted return vectors X (M x N) into k regimes.

    `init` optionally warm-starts from previous centroids (first candidate).

    Returns (labels, centroids, inertia) where inertia is the sum of W_p
    distances of each window to its barycenter.
    """
    rng = np.random.default_rng(seed)
    best = None
    candidates = []
    if init is not None:
        candidates.append(np.asarray(init, dtype=float).copy())
    for _ in range(max(0, n_init - len(candidates))):
        candidates.append(_kmeanspp_init(X, k, p, rng))
    for C in candidates:
        labels = np.zeros(X.shape[0], dtype=int)
        for _ in range(max_iter):
            D = wasserstein_p_matrix(X, C, p)
            labels = D.argmin(axis=1)
            new_C = C.copy()
            for j in range(k):
                members = X[labels == j]
                if len(members) == 0:  # re-seed empty cluster with farthest point
                    far = D.min(axis=1).argmax()
                    new_C[j] = X[far]
                else:
                    new_C[j] = wasserstein_barycenter(members, p)
            shift = wasserstein_p_matrix(C, new_C, p).diagonal().sum()
            C = new_C
            if shift < tol:
                break
        D = wasserstein_p_matrix(X, C, p)
        labels = D.argmin(axis=1)
        inertia = D.min(axis=1).sum()
        if best is None or inertia < best[2]:
            best = (labels, C.copy(), inertia)
    return best


# ---------------------------------------------------------------------------
# Soft assignment (posterior-like probabilities) and semantic labels
# ---------------------------------------------------------------------------

def soft_assign(X: np.ndarray, C: np.ndarray, p: float = 2.0) -> np.ndarray:
    """Distance-based soft membership: softmax over negative squared distances,
    with temperature = median squared distance (scale-free).

    Returns M x K probability matrix (rows sum to 1)."""
    D = wasserstein_p_matrix(X, C, p)
    d2 = D ** 2
    temp = max(np.median(d2), 1e-12)
    z = -d2 / temp
    z = z - z.max(axis=1, keepdims=True)
    w = np.exp(z)
    return w / w.sum(axis=1, keepdims=True)


def semantic_labels(centroids: np.ndarray) -> tuple[dict, dict]:
    """Map cluster ids to Bull / Choppy / Bear semantics from centroid stats.

    Rule (rank-based, robust to label switching between refits):
      - Bull : highest centroid mean return
      - Bear : most negative centroid mean return; if k == 2 the remaining
               cluster with lower mean is Choppy/Bear by vol rank
      - order by mean return: top = Bull, bottom = Bear, middle(s) = Choppy
    Returns (id -> name, id -> {'ann_ret', 'ann_vol'}).
    """
    k = centroids.shape[0]
    mu = centroids.mean(axis=1)
    order = np.argsort(mu)  # ascending: most negative first
    names = {}
    for rank, cid in enumerate(order):
        if rank == 0:
            names[cid] = "Bear" if k >= 3 else "Bear"
        elif rank == k - 1:
            names[cid] = "Bull"
        else:
            names[cid] = "Choppy"
    stats = {
        cid: {
            "ann_ret": float(mu[cid] * 252),
            "ann_vol": float(centroids[cid].std() * np.sqrt(252)),
        }
        for cid in range(k)
    }
    return names, stats


# ---------------------------------------------------------------------------
# Cluster quality: MMD self-similarity (paper Eq. 13, Gaussian kernel)
# ---------------------------------------------------------------------------

def mmd2_biased(x: np.ndarray, y: np.ndarray, sigma: float = 0.1) -> float:
    """Biased MMD^2 with Gaussian kernel between two samples (1-D)."""
    kxx = np.exp(-((x[:, None] - x[None, :]) ** 2) / (2 * sigma ** 2))
    kyy = np.exp(-((y[:, None] - y[None, :]) ** 2) / (2 * sigma ** 2))
    kxy = np.exp(-((x[:, None] - y[None, :]) ** 2) / (2 * sigma ** 2))
    return float(kxx.mean() + kyy.mean() - 2 * kxy.mean())


def cluster_self_similarity(X: np.ndarray, labels: np.ndarray, sigma: float = 0.1,
                            max_pairs: int = 200, seed: int = 0) -> dict:
    """Median pairwise MMD^2 within each cluster (lower = more homogeneous),
    following the paper's self-similarity score. Windows are flattened
    samples of returns."""
    rng = np.random.default_rng(seed)
    out = {}
    for j in np.unique(labels):
        members = X[labels == j]
        n = len(members)
        if n < 2:
            out[int(j)] = float("nan")
            continue
        vals = []
        idx = rng.choice(n, size=min(n, max_pairs), replace=False)
        for a in range(len(idx) - 1):
            for b in range(a + 1, len(idx)):
                vals.append(
                    mmd2_biased(members[idx[a]], members[idx[b]], sigma)
                )
        out[int(j)] = float(np.median(vals))
    return out
