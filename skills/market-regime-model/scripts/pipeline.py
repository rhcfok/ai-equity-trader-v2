"""
Pipeline: data loading, rolling-window construction, and *walk-forward*
(out-of-sample) regime detection with hysteresis for the three models
(WK-means, MK-means, Gaussian HMM).

Honesty protocol
----------------
- A window ending on day t only uses returns up to day t.
- Models are fitted on data up to a refit day, then applied online day by day
  until the next refit. No future data enters any probability or label.
- HMM probabilities are true *filtered* (forward-only) probabilities: the
  hmmlearn model is used for EM only; online filtering is done with an
  explicit forward recursion on the fitted parameters.
- Hysteresis: the displayed regime only switches to the top-probability
  regime once its probability has stayed >= theta for `m_conf` consecutive
  days, or immediately if it exceeds `escape` (crash escape hatch).
"""

from __future__ import annotations

import os
import numpy as np
import pandas as pd
import yfinance as yf

from wkmeans import wkmeans, soft_assign, semantic_labels
from baselines import MKMeans, HMMRegime

CANON = ["Bull", "Choppy", "Bear"]  # canonical column order, k=3
CANON2 = ["Bull", "Bear"]           # k=2


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_spy(start="1993-01-01", end=None, cache_path="out/spy_daily.csv",
             ticker="SPY") -> pd.DataFrame:
    """Download (or cache) adjusted daily closes for a ticker."""
    if end is None:
        # pad the exclusive end so the most recent session is never clipped
        end = (pd.Timestamp.today() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    # freshness rule: prefer a fresh download every run (the daily job runs
    # after the prior US close); cache is only a network-failure fallback.
    try:
        df = yf.download(ticker, start=start, end=end, progress=False,
                         auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # the newest bar often arrives with an adjusted Close of NaN while
        # volume is already published; fill it from the unadjusted series
        # (exact whenever no ex-dividend date sits in the gap)
        if len(df) and df["Close"].isna().any():
            raw = yf.download(ticker, start=start, end=end, progress=False,
                              auto_adjust=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            df["Close"] = df["Close"].fillna(
                raw["Close"].reindex(df.index))
        df = df[["Close"]].dropna()
        if len(df):
            df.to_csv(cache_path)
            return df
        raise ValueError(f"empty download for {ticker}")
    except Exception:
        if os.path.exists(cache_path):
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            print(f"WARNING: using cached data for {ticker} "
                  f"(last {df.index[-1].date()}); download failed")
            return df
        raise


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close).diff().dropna()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def make_windows(r: np.ndarray, w: int):
    """X[i] = sorted returns of window ending at day i (i = w-1 .. T-1).
    Returns (X, day_idx)."""
    T = len(r)
    idx = np.arange(w - 1, T)
    shape = (len(idx), w)
    strides = (r.strides[0], r.strides[0])
    W = np.lib.stride_tricks.as_strided(r, shape=shape, strides=strides)
    X = np.sort(W, axis=1)
    return np.ascontiguousarray(X), idx


def canonical_perm(names: dict, k: int) -> list:
    """Permutation taking cluster-id column order to canonical order."""
    order = CANON if k >= 3 else CANON2
    id_of = {}
    for cid, nm in names.items():
        if nm in ("Choppy",) and k == 2:
            nm = "Bear"
        id_of.setdefault(nm, cid)
    return [id_of[nm] for nm in order]


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------

def apply_hysteresis(probs: np.ndarray, theta: float = 0.55, m_conf: int = 3,
                     escape: float = 0.85) -> np.ndarray:
    """Filtered regime labels from probability matrix (T x k).
    Columns must be in canonical order."""
    T = probs.shape[0]
    labels = np.zeros(T, dtype=int)
    state = int(probs[0].argmax())
    pending, count = None, 0
    for t in range(T):
        top = int(probs[t].argmax())
        if top != state and probs[t, top] >= theta:
            if top == pending:
                count += 1
            else:
                pending, count = top, 1
            if count >= m_conf or probs[t, top] >= escape:
                state, pending, count = top, None, 0
        elif top == state:
            pending, count = None, 0
        labels[t] = state
    return labels


# ---------------------------------------------------------------------------
# Walk-forward WK-means / MK-means (window-distribution models)
# ---------------------------------------------------------------------------

def walk_forward_windows(returns: pd.Series, w=40, k=3, p=2.0,
                         train_years=12, refit_days=252,
                         theta=0.55, m_conf=3, escape=0.85,
                         kind="wkmeans", seed=42):
    r = returns.values
    dates = returns.index
    X, day_idx = make_windows(r, w)
    train_end = int(np.searchsorted(day_idx, 252 * train_years))
    train_end = max(train_end, 30)

    T = len(day_idx)
    probs = np.full((T, k), np.nan)
    fitted_at = []
    warm = None

    i = train_end
    next_refit = i
    labs = names = perm = None
    while i < T:
        if i >= next_refit:
            X_fit = X[: i + 1]
            if kind == "wkmeans":
                # warm start from previous centroids after the first fit:
                # faster refits and more stable cluster identities over time
                labs, C, _ = wkmeans(X_fit, k, p=p, seed=seed, init=warm,
                                     n_init=2 if warm is None else 1,
                                     max_iter=100)
                warm = C
                posterior = lambda Xi: soft_assign(Xi, C, p)
            elif kind == "mkmeans":
                mk = MKMeans(k=k, seed=seed).fit(X_fit)
                posterior = mk.soft_assign
                C = mk.centroid_stats
            else:
                raise ValueError(kind)
            names, _ = semantic_labels(C)
            perm = canonical_perm(names, k)
            fitted_at.append(dates[day_idx[i]])
            next_refit = i + refit_days
        probs[i] = posterior(X[i][None, :])[0][perm]
        i += 1

    labels = apply_hysteresis(probs[train_end:], theta, m_conf, escape)
    out = pd.DataFrame(
        probs[train_end:], index=dates[day_idx[train_end:]],
        columns=CANON[:k] if k >= 3 else CANON2,
    )
    out["regime"] = [ (CANON if k >= 3 else CANON2)[j] for j in labels ]
    out["fitted"] = out.index.isin(fitted_at)
    return out


# ---------------------------------------------------------------------------
# Walk-forward Gaussian HMM with exact forward filtering
# ---------------------------------------------------------------------------

def forward_filter(r: np.ndarray, A: np.ndarray, mu: np.ndarray, sig2: np.ndarray,
                   pi0: np.ndarray) -> np.ndarray:
    """Forward-only filtered posteriors, T x k."""
    T = len(r)
    k = len(mu)
    ll = (
        -0.5 * np.log(2 * np.pi * sig2)[None, :]
        - 0.5 * (r[:, None] - mu[None, :]) ** 2 / sig2[None, :]
    )
    a = pi0 * np.exp(ll[0])
    a /= a.sum()
    out = np.zeros((T, k))
    out[0] = a
    for t in range(1, T):
        a = (a @ A) * np.exp(ll[t])
        s = a.sum()
        if s <= 0 or not np.isfinite(s):
            a = np.full(k, 1.0 / k)
        else:
            a /= s
        out[t] = a
    return out


def walk_forward_hmm(returns: pd.Series, k=3, train_years=12, refit_days=126,
                     fit_window=2000, theta=0.55, m_conf=3, escape=0.85,
                     seed=42):
    r = returns.values
    dates = returns.index
    T = len(r)
    start = int(252 * train_years)

    # sequential filtering: refit EM on trailing data at refit points,
    # then filter forward day by day with the frozen parameters.
    probs = np.full((T, k), np.nan)
    A = mu = sig2 = pi0 = perm = None
    next_refit = start
    i = start
    while i < T:
        if i >= next_refit:
            lo = max(0, i - fit_window)
            hmm = HMMRegime(k=k, seed=seed).fit(r[lo:i + 1])
            m = hmm.model
            A = m.transmat_.copy()
            mu = m.means_.ravel().copy()
            sig2 = np.maximum(m.covars_.ravel().copy(), 1e-10)
            pi0 = m.startprob_.copy()
            order = np.argsort(mu)
            perm = np.array([order[-1], order[1], order[0]]) if k >= 3 \
                else np.array([order[-1], order[0]])
            next_refit = i + refit_days
        # filter from last refit up to current day i
        seg_start = max(start, i - ((i - start) % refit_days))
        seg = r[seg_start:i + 1]
        P = forward_filter(seg, A, mu, sig2, pi0)
        probs[i] = P[-1][perm]
        i += 1

    labels = apply_hysteresis(probs[start:], theta, m_conf, escape)
    cols = CANON if k >= 3 else CANON2
    out = pd.DataFrame(probs[start:], index=dates[start:], columns=cols)
    out["regime"] = [cols[j] for j in labels]
    return out


# ---------------------------------------------------------------------------
# Hybrid regime: WK-means distribution state x trend state
# ---------------------------------------------------------------------------

def trend_state(close: pd.Series, mom: int = 126, band: float = 0.02,
                confirm: int = 0) -> pd.Series:
    """Schmitt-trigger trend state from `mom`-day log return.

    State flips to -1 only when momentum < -band, and back to +1 only when
    momentum > +band. The deadband kills whipsaw when momentum hovers near
    zero. +1 = uptrend, -1 = downtrend. Uses trailing data only.

    Grid-tested optimum on SPY 2005-2026: mom=126, band=2% (see REPORT).
    """
    m = np.log(close).diff(mom)
    s = pd.Series(index=close.index, dtype=float)
    state = 1.0
    for d, v in m.items():
        if not np.isnan(v):
            if v > band:
                state = 1.0
            elif v < -band:
                state = -1.0
        s[d] = state
    if confirm > 0:
        s = s.rolling(2 * confirm + 1, min_periods=1).median().shift(confirm)
        s = s.ffill().fillna(1.0)
    return pd.Series(np.where(s >= 0, 1, -1), index=close.index)


def hybrid_regime(oos: pd.DataFrame, close: pd.Series, mom: int = 126,
                  band: float = 0.02, confirm: int = 0) -> pd.Series:
    """Combine the WK-means distribution state with the trend state.

    Rule (drift x distribution-shape):
        dist=Bull             & trend up   -> Bull
        dist=Bull             & trend down -> Choppy  (late/defensive bull)
        dist in {Choppy,Bear} & trend down -> Bear    (incl. grind-down bears
                                                       the distribution alone
                                                       cannot separate)
        dist in {Choppy,Bear} & trend up   -> Choppy
    """
    dist = oos["regime"].reindex(oos.index)
    tr = trend_state(close, mom, band, confirm).reindex(oos.index)
    out = []
    for d, t in zip(dist.values, tr.values):
        if d == "Bull":
            out.append("Bull" if t > 0 else "Choppy")
        else:
            out.append("Bear" if t < 0 else "Choppy")
    return pd.Series(out, index=oos.index, name="regime")


# ---------------------------------------------------------------------------
# Transition events
# ---------------------------------------------------------------------------

def transition_events(regime: pd.Series) -> pd.DataFrame:
    ch = regime[regime.ne(regime.shift())]
    ch = ch.iloc[1:]  # drop the first state
    prev = regime.shift().reindex(ch.index)
    return pd.DataFrame({"from": prev, "to": ch, "date": ch.index}).reset_index(drop=True)


KNOWN_EVENTS = [
    ("1998-08-17", "LTCM / Russia default"),
    ("2000-03-24", "Dot-com peak"),
    ("2001-09-11", "9/11"),
    ("2002-10-09", "Dot-com bear bottom"),
    ("2007-10-09", "Pre-GFC all-time high"),
    ("2008-09-15", "Lehman collapse"),
    ("2009-03-09", "GFC bottom"),
    ("2010-04-26", "Greek crisis / flash crash"),
    ("2011-08-05", "S&P US downgrade"),
    ("2015-08-11", "China devaluation"),
    ("2018-02-05", "Volmageddon"),
    ("2018-12-24", "Q4-2018 selloff bottom"),
    ("2020-02-19", "COVID pre-crash high"),
    ("2020-03-23", "COVID bottom"),
    ("2022-01-03", "2022 bear starts"),
    ("2022-10-12", "2022 bear bottom"),
    ("2023-10-27", "2023 correction bottom"),
    ("2025-04-07", "Tariff shock bottom"),
    ("2026-11-05", "Sharp -4.4% single-day drop"),
]
