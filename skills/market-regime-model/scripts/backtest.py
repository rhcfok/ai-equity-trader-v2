"""
Regime-strategy backtest + transition-quality metrics.

Position map (default): Bull -> 100% long, Choppy -> 25% long, Bear -> cash.
Signals decided on day t close are applied to day t+1's return (1-day lag,
no look-ahead), with proportional transaction costs on turnover.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

POSITIONS = {"Bull": 1.0, "Choppy": 0.25, "Bear": 0.0}


def backtest(regime: pd.Series, ret: pd.Series, positions=None, cost_bps=2.0):
    positions = positions or POSITIONS
    pos = regime.map(positions).astype(float)
    # apply with 1-day lag: position known at close t earns day t+1 return
    pos_lag = pos.shift(1).fillna(0.0)
    turnover = pos_lag.diff().abs().fillna(pos_lag.abs())
    net = pos_lag * ret - turnover * cost_bps / 1e4
    equity = (1 + net).cumprod()
    return pd.DataFrame({"ret": net, "equity": equity, "pos": pos_lag})


def perf_stats(ret: pd.Series, pos: pd.Series = None, freq=252) -> dict:
    r = ret.dropna()
    eq = (1 + r).cumprod()
    years = len(r) / freq
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(freq)
    sharpe = (r.mean() * freq) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd < 0 else np.nan
    out = {
        "CAGR": cagr, "AnnVol": vol, "Sharpe": sharpe,
        "MaxDD": dd, "Calmar": calmar,
    }
    if pos is not None:
        out["Switches/yr"] = (pos.diff().abs() > 0).sum() / years
    return out


# ---------------------------------------------------------------------------
# Transition quality vs. ex-post bear episodes
# ---------------------------------------------------------------------------

def drawdown_episodes(close: pd.Series, threshold=-0.20, early=-0.10):
    """Extract peak-to-trough episodes with max drawdown <= threshold.
    peak_date = last all-time-high day before the slide; early_date = first
    close below `early` drawdown (early-warning point)."""
    eq = (close / close.cummax() - 1)
    episodes = []
    in_dd, peak_i, trough_i, early_date = False, None, None, None
    last_peak = 0
    for i, (d, v) in enumerate(eq.items()):
        if v >= -1e-9:  # at a new high: remember the true peak position
            last_peak = i
        if not in_dd:
            if v <= early:
                in_dd, peak_i = True, last_peak
                trough_i, early_date = i, d
        else:
            if v < eq.iloc[trough_i]:
                trough_i = i
            if v >= -0.02:  # episode closed (recovered above -2%)
                mdd = eq.iloc[trough_i]
                if mdd <= threshold:
                    episodes.append({
                        "peak_date": eq.index[peak_i],
                        "early_date": early_date,
                        "trough_date": eq.index[trough_i],
                        "max_dd": float(mdd),
                    })
                in_dd = False
    if in_dd and eq.iloc[trough_i] <= threshold:
        episodes.append({
            "peak_date": eq.index[peak_i], "early_date": early_date,
            "trough_date": eq.index[trough_i], "max_dd": float(eq.iloc[trough_i]),
        })
    return pd.DataFrame(episodes)


def detection_lags(regime: pd.Series, episodes: pd.DataFrame,
                   window_days=40) -> pd.DataFrame:
    """For each ex-post bear episode: when did the model first flag Bear
    between (peak - 5d) and (trough + window_days)?
    Lag measured from the early-warning date (first crossing of -10%)."""
    rows = []
    for _, ep in episodes.iterrows():
        lo = regime.index[regime.index.searchsorted(ep["peak_date"]) - 5] \
            if regime.index.searchsorted(ep["peak_date"]) >= 5 else regime.index[0]
        hi = ep["trough_date"] + pd.Timedelta(days=window_days)
        seg = regime[(regime.index >= lo) & (regime.index <= hi)]
        flagged = seg[seg == "Bear"]
        if len(flagged):
            flag = flagged.index[0]
            lag = (flag - ep["early_date"]).days
            status = "detected"
        else:
            flag, lag, status = pd.NaT, np.nan, "missed"
        rows.append({**ep.to_dict(), "bear_flag": flag,
                     "lag_days_vs_-10%": lag, "status": status})
    return pd.DataFrame(rows)
