#!/usr/bin/env python3
"""Faithful Python port of jdehorty's "Machine Learning: Lorentzian Classification"
v2.0 plus the classic Supertrend, as a single signal engine.

Pine sources ported
-------------------
* "Machine Learning: Lorentzian Classification" v2.0 (Pine v6) - the KNN with
  Lorentzian distance, prediction filters, kernel filters, entry/exit state
  machine (user-provided .txt).
* jdehorty/MLExtensions/2 - n_rsi / n_wt / n_cci / n_adx, regime_filter,
  filter_adx, filter_volatility, rescale, normalize. Ported from the official
  TradingView function docs (script ia5ozyMF) cross-checked against three
  independent ports: danielgonzalez3/trade-bot, KevinGastelum/MyQuantsFinance,
  techfreaque/octobot-lorentzian-classification (ml_extensions_2).
* jdehorty/KernelFunctions/2 - rationalQuadratic / gaussian kernels
  (OctoBot kernel_functions + KevinGastelum ports agree).
* Classic Supertrend (Pine v4: ATR 10, hl2 source, multiplier 3, RMA ATR)
  (user-provided .txt).

Causality
---------
Every series uses only data up to its own bar. The single Pine construct that
cannot be reproduced verbatim is MLExtensions `normalize` (min-max to [0,1]
over an unbounded range): a full-sample min-max would repaint/look ahead, so
the expanding-window form (min/max since the first bar) is used - the only
causal semantics consistent with the docstring and Pine's `var` accumulator
idiom. On long histories it converges to the full-sample values.

Strategy combination (the runner's default)
-------------------------------------------
* LONG  = LDC startLongTrade  AND Supertrend direction bullish
* SHORT = LDC startShortTrade AND Supertrend direction bearish
* EXIT  = LDC strict 4-bar exit OR opposite signal OR Supertrend flip
(`use_supertrend_filter=False` yields pure LDC alerts.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pandas_ta as ta

# ---------------------------------------------------------------------------
# Settings (all defaults match the Pine inputs)
# ---------------------------------------------------------------------------
@dataclass
class LDCSettings:
    neighbors_count: int = 8
    max_bars_back: int = 2000
    feature_count: int = 5
    include_full_history: bool = False
    # Feature engineering: (kind, paramA, paramB) - Pine defaults
    features: tuple = (("RSI", 14, 1), ("WT", 10, 11), ("CCI", 20, 1),
                       ("ADX", 20, 2), ("RSI", 9, 1))
    use_volatility_filter: bool = True
    use_regime_filter: bool = True
    regime_threshold: float = -0.1
    use_adx_filter: bool = False
    adx_threshold: int = 20
    use_ema_filter: bool = False
    ema_period: int = 200
    use_sma_filter: bool = False
    sma_period: int = 200
    use_kernel_filter: bool = True
    use_kernel_smoothing: bool = False
    kernel_lookback: int = 8            # h
    kernel_relative_weight: float = 8.0  # r
    kernel_regression_level: int = 25    # x (startAtBar)
    kernel_lag: int = 2                  # lag


@dataclass
class SupertrendSettings:
    atr_length: int = 10
    multiplier: float = 3.0


# ---------------------------------------------------------------------------
# MLExtensions/2 helpers
# ---------------------------------------------------------------------------
def _rescale(src: pd.Series, old_min, old_max, new_min=0.0, new_max=1.0) -> pd.Series:
    """MLExtensions rescale: bounded range -> bounded range (causal)."""
    span = old_max - old_min
    if isinstance(span, pd.Series):
        span = span.mask(span.abs() < 1e-10, 1e-10)
    elif abs(span) < 1e-10:
        span = 1e-10
    return new_min + (new_max - new_min) * (src - old_min) / span


def _normalize(src: pd.Series) -> pd.Series:
    """MLExtensions normalize: unbounded range -> [0, 1].

    Expanding min/max since the first bar - the causal form of the Pine
    `var` accumulator (see module docstring)."""
    lo = src.expanding(min_periods=1).min()
    hi = src.expanding(min_periods=1).max()
    return _rescale(src, lo, hi, 0.0, 1.0)


def _cci_on_series(src: pd.Series, length: int) -> pd.Series:
    """Pine ta.cci(source, length): deviation of the given series from its SMA."""
    sma = ta.sma(src, length)
    mad = (src - sma).abs().rolling(length).mean()
    return (src - sma) / (0.015 * mad)


def _wavetrend(src: pd.Series, n1: int, n2: int) -> tuple[pd.Series, pd.Series]:
    """WaveTrend Classic: channel length n1, averaging length n2."""
    esa = ta.ema(src, n1)
    d = ta.ema((src - esa).abs(), n1)
    ci = (src - esa) / (0.015 * d)
    wt1 = ta.ema(ci, n2)
    wt2 = ta.sma(wt1, 4)
    return wt1, wt2


def compute_features(df: pd.DataFrame, s: LDCSettings) -> np.ndarray:
    """Feature matrix (n_bars x feature_count) exactly as ml.n_* build them."""
    close, high, low = df["close"], df["high"], df["low"]
    hlc3 = (high + low + close) / 3.0
    cols = []
    for kind, a, b in list(s.features)[: s.feature_count]:
        if kind == "RSI":
            f = _rescale(ta.ema(ta.rsi(close, length=a), length=b), 0, 100, 0, 1)
        elif kind == "WT":
            wt1, wt2 = _wavetrend(hlc3, a, b)
            f = _normalize(wt1 - wt2)
        elif kind == "CCI":
            f = _normalize(ta.ema(_cci_on_series(close, a), length=b))
        elif kind == "ADX":
            adx_df = ta.adx(high, low, close, length=a)
            adx = adx_df[next(c for c in adx_df.columns if c.startswith("ADX_"))]
            f = _rescale(adx, 0, 100, 0, 1)
        else:
            raise ValueError(f"unsupported feature kind: {kind}")
        cols.append(np.asarray(f, dtype=float))
    return np.column_stack(cols)


# ---------------------------------------------------------------------------
# KernelFunctions/2
# ---------------------------------------------------------------------------
def kernel_rational_quadratic(src: pd.Series, lookback: int, relative_weight: float,
                              start_at_bar: int) -> pd.Series:
    """yhat over offsets 0..start_at_bar with w = (1+i^2/(2*rw*lb^2))^(-rw)."""
    offsets = np.arange(0, start_at_bar + 1, dtype=float)
    w = (1.0 + offsets**2 / (2.0 * relative_weight * lookback**2)) ** (-relative_weight)
    acc = pd.Series(0.0, index=src.index)
    for i, wi in zip(offsets, w):
        acc = acc + wi * src.shift(int(i))
    return acc / w.sum()


def kernel_gaussian(src: pd.Series, lookback: int, start_at_bar: int) -> pd.Series:
    """yhat over offsets 0..start_at_bar with w = exp(-i^2/(2*lb^2))."""
    offsets = np.arange(0, start_at_bar + 1, dtype=float)
    w = np.exp(-(offsets**2) / (2.0 * lookback**2))
    acc = pd.Series(0.0, index=src.index)
    for i, wi in zip(offsets, w):
        acc = acc + wi * src.shift(int(i))
    return acc / w.sum()


# ---------------------------------------------------------------------------
# MLExtensions/2 filters
# ---------------------------------------------------------------------------
def filter_volatility(df: pd.DataFrame, min_length: int = 1, max_length: int = 10,
                      enabled: bool = True) -> pd.Series:
    """ATR(minLength) > ATR(maxLength) - volatility expanding."""
    if not enabled:
        return pd.Series(True, index=df.index)
    fast = ta.atr(df["high"], df["low"], df["close"], length=min_length, mamode="rma")
    slow = ta.atr(df["high"], df["low"], df["close"], length=max_length, mamode="rma")
    return fast > slow


def regime_filter(df: pd.DataFrame, src: pd.Series, threshold: float,
                  enabled: bool = True) -> pd.Series:
    """KLMF adaptive-average slope vs its EMA(200), thresholded (Ehlers-style)."""
    if not enabled:
        return pd.Series(True, index=df.index)
    o = src.to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    n = len(o)
    klmf = np.empty(n)
    slope = np.empty(n)
    v1 = v2 = k = 0.0
    for i in range(n):
        prev_o = o[i - 1] if i else o[0]
        v1 = 0.2 * (o[i] - prev_o) + 0.8 * v1
        v2 = 0.1 * (h[i] - l[i]) + 0.8 * v2
        omega = abs(v1 / v2) if v2 != 0.0 else 0.0
        alpha = (-omega**2 + math.sqrt(omega**4 + 16.0 * omega**2)) / 8.0
        k = alpha * o[i] + (1.0 - alpha) * k
        klmf[i] = k
        slope[i] = abs(k - (klmf[i - 1] if i else 0.0))
    slope_s = pd.Series(slope, index=df.index)
    # pandas_ta returns None (object dtype) in the un-warmed region when the
    # series is shorter than the EMA length - coerce to NaN.
    ema_slope = pd.to_numeric(ta.ema(slope_s, length=200), errors="coerce")
    nsd = (slope_s - ema_slope) / ema_slope
    return nsd >= threshold


def filter_adx(df: pd.DataFrame, length: int = 14, adx_threshold: int = 20,
               enabled: bool = True) -> pd.Series:
    if not enabled:
        return pd.Series(True, index=df.index)
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=length)
    adx = adx_df[next(c for c in adx_df.columns if c.startswith("ADX_"))]
    return adx > adx_threshold


# ---------------------------------------------------------------------------
# Supertrend (Pine v4 classic: hl2 source, RMA ATR)
# ---------------------------------------------------------------------------
def supertrend(df: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    src = ((high + low) / 2.0).to_numpy()
    c = close.to_numpy()
    atr = ta.atr(high, low, close, length=length, mamode="rma").to_numpy()
    n = len(df)
    st_dir = np.empty(n, dtype=int)
    up = np.full(n, np.nan)
    dn = np.full(n, np.nan)
    up_prev = dn_prev = np.nan
    t = 1
    for i in range(n):
        if np.isnan(atr[i]):
            st_dir[i] = t
            continue
        up_raw = src[i] - multiplier * atr[i]
        dn_raw = src[i] + multiplier * atr[i]
        up1 = up_raw if np.isnan(up_prev) else up_prev   # nz(up[1], up)
        dn1 = dn_raw if np.isnan(dn_prev) else dn_prev
        if i and not np.isnan(c[i - 1]):
            up_band = max(up_raw, up1) if c[i - 1] > up1 else up_raw
            dn_band = min(dn_raw, dn1) if c[i - 1] < dn1 else dn_raw
            if t == -1 and c[i] > dn1:
                t = 1
            elif t == 1 and c[i] < up1:
                t = -1
        else:
            up_band, dn_band = up_raw, dn_raw
        up[i], dn[i] = up_band, dn_band
        st_dir[i] = t
        up_prev, dn_prev = up_band, dn_band
    out = pd.DataFrame({"st_dir": st_dir, "st_up": up, "st_dn": dn}, index=df.index)
    out["st_flip_up"] = (out["st_dir"] == 1) & (out["st_dir"].shift(1) == -1)
    out["st_flip_dn"] = (out["st_dir"] == -1) & (out["st_dir"].shift(1) == 1)
    return out


# ---------------------------------------------------------------------------
# Core ML logic: approximate nearest neighbours with Lorentzian distance
# ---------------------------------------------------------------------------
def _knn_predictions(F: np.ndarray, labels: np.ndarray, s: LDCSettings) -> np.ndarray:
    """Bar-by-bar ANN scan, replicating the Pine state exactly:
    persistent neighbour arrays across bars, lastDistance reset per bar,
    every 4th bar index excluded, eviction at k+1 with lastDistance set to
    distances[round(k*3/4)]."""
    n = len(labels)
    k = s.neighbors_count
    last_bar_index = n - 1
    max_bb_index = last_bar_index - s.max_bars_back if last_bar_index >= s.max_bars_back else 0
    start_index = 0 if s.include_full_history else max_bb_index
    eviction_idx = round(k * 3 / 4)

    dist_arr: list[float] = []
    pred_arr: list[float] = []
    out = np.zeros(n)
    eligible = (np.arange(n) % 4) != 0

    for b in range(max_bb_index, n):
        hi = min(s.max_bars_back - 1, b)
        if hi < start_index:
            out[b] = sum(pred_arr)
            continue
        diffs = np.abs(F[b] - F[start_index:hi + 1])
        d_vec = np.log1p(diffs).sum(axis=1)
        last_distance = -1.0
        for pos in np.nonzero(eligible[start_index:hi + 1])[0]:
            d = d_vec[pos]
            if d >= last_distance:                 # NaN fails naturally
                last_distance = float(d)
                dist_arr.append(float(d))
                pred_arr.append(round(labels[start_index + pos]))
                if len(pred_arr) > k:
                    last_distance = dist_arr[eviction_idx]
                    dist_arr.pop(0)
                    pred_arr.pop(0)
        out[b] = sum(pred_arr)
    return out


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------
def lorentzian_classification(df: pd.DataFrame, s: LDCSettings | None = None) -> pd.DataFrame:
    """Run the LDC indicator over OHLCV `df` (columns open/high/low/close).

    Returns a frame indexed like `df` with prediction/signal/filter/entry/exit
    columns evaluated bar-by-bar (no lookahead)."""
    s = s or LDCSettings()
    close = df["close"]
    n = len(df)

    F = compute_features(df, s)

    # Training labels - Pine: src[4] < src[0] ? short : src[4] > src[0] ? long.
    # At bar i that is close[i-4] vs close[i]: a RISING 4-bar move into the bar
    # carries the SHORT label, a falling one the LONG label (the indicator's
    # own convention - do not "fix" it to intuitive momentum).
    c = close.to_numpy(dtype=float)
    labels = np.zeros(n, dtype=int)
    if n > 4:
        labels[4:] = np.where(c[4:] < c[:-4], 1, np.where(c[4:] > c[:-4], -1, 0))

    prediction = pd.Series(_knn_predictions(F, labels, s), index=df.index)

    # Prediction filters
    f_vol = filter_volatility(df, 1, 10, s.use_volatility_filter)
    f_regime = regime_filter(df, (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0,
                             s.regime_threshold, s.use_regime_filter)
    f_adx = filter_adx(df, 14, s.adx_threshold, s.use_adx_filter)
    filter_all = (f_vol & f_regime & f_adx).fillna(False).to_numpy()

    # Kernel regression filters
    yhat1 = kernel_rational_quadratic(close, s.kernel_lookback, s.kernel_relative_weight,
                                      s.kernel_regression_level)
    yhat2 = kernel_gaussian(close, s.kernel_lookback - s.kernel_lag, s.kernel_regression_level)
    is_bullish_rate = yhat1 > yhat1.shift(1)
    is_bearish_rate = yhat1 < yhat1.shift(1)
    is_bullish_smooth = yhat2 >= yhat1
    is_bearish_smooth = yhat2 <= yhat1
    if s.use_kernel_smoothing:
        is_bullish, is_bearish = is_bullish_smooth, is_bearish_smooth
    else:
        is_bullish, is_bearish = is_bullish_rate, is_bearish_rate
    if not s.use_kernel_filter:
        is_bullish = pd.Series(True, index=df.index)
        is_bearish = pd.Series(True, index=df.index)

    # EMA / SMA trend filters (off by default)
    if s.use_ema_filter:
        ema = ta.ema(close, length=s.ema_period)
        is_ema_up, is_ema_dn = close > ema, close < ema
    else:
        is_ema_up = is_ema_dn = pd.Series(True, index=df.index)
    if s.use_sma_filter:
        sma = ta.sma(close, length=s.sma_period)
        is_sma_up, is_sma_dn = close > sma, close < sma
    else:
        is_sma_up = is_sma_dn = pd.Series(True, index=df.index)

    # Signal state machine
    signal = np.zeros(n, dtype=int)
    prev = 0
    fa = filter_all
    for b in range(n):
        if prediction.iloc[b] > 0 and fa[b]:
            prev = 1
        elif prediction.iloc[b] < 0 and fa[b]:
            prev = -1
        signal[b] = prev
    signal_s = pd.Series(signal, index=df.index)

    change = signal_s.diff().fillna(0.0) != 0.0        # ta.change(signal) != 0
    bars_held = pd.Series(0, index=df.index, dtype=int)
    held = 0
    for b in range(n):
        held = 0 if change.iloc[b] else held + 1
        bars_held.iloc[b] = held

    is_buy_signal = (signal_s == 1) & is_ema_up & is_sma_up
    is_sell_signal = (signal_s == -1) & is_ema_dn & is_sma_dn
    is_new_buy = is_buy_signal & change
    is_new_sell = is_sell_signal & change

    start_long = is_new_buy & is_bullish & is_ema_up & is_sma_up
    start_short = is_new_sell & is_bearish & is_ema_dn & is_sma_dn

    # Strict (default) exits: 4 bars after entry, or early flip while held < 4.
    sig4 = signal_s.shift(4)
    start_long4 = start_long.shift(4).fillna(False)
    start_short4 = start_short.shift(4).fillna(False)
    is_last_signal_buy = (sig4 == 1) & is_ema_up.shift(4).fillna(True) & is_sma_up.shift(4).fillna(True)
    is_last_signal_sell = (sig4 == -1) & is_ema_dn.shift(4).fillna(True) & is_sma_dn.shift(4).fillna(True)
    held_four = bars_held == 4
    held_less = (bars_held > 0) & (bars_held < 4)
    end_long = ((held_four & is_last_signal_buy) | (held_less & is_new_sell & is_last_signal_buy)) & start_long4
    end_short = ((held_four & is_last_signal_sell) | (held_less & is_new_buy & is_last_signal_sell)) & start_short4

    out = pd.DataFrame({
        "prediction": prediction,
        "signal": signal_s,
        "filter_volatility": f_vol.fillna(False),
        "filter_regime": f_regime.fillna(False),
        "filter_adx": f_adx.fillna(False),
        "kernel_bullish": is_bullish.fillna(False),
        "kernel_bearish": is_bearish.fillna(False),
        "bars_held": bars_held,
        "start_long": start_long.fillna(False),
        "start_short": start_short.fillna(False),
        "end_long": end_long.fillna(False),
        "end_short": end_short.fillna(False),
    }, index=df.index)
    return out


def compute_signals(df: pd.DataFrame, s: LDCSettings | None = None,
                    st: SupertrendSettings | None = None,
                    use_supertrend_filter: bool = True,
                    exit_on_st_flip: bool = True) -> pd.DataFrame:
    """LDC + Supertrend combined strategy frame (see module docstring)."""
    st = st or SupertrendSettings()
    out = lorentzian_classification(df, s)
    st_df = supertrend(df, st.atr_length, st.multiplier)
    out["st_dir"] = st_df["st_dir"]
    out["st_flip_up"] = st_df["st_flip_up"]
    out["st_flip_dn"] = st_df["st_flip_dn"]

    if use_supertrend_filter:
        out["entry_long"] = out["start_long"] & (out["st_dir"] == 1)
        out["entry_short"] = out["start_short"] & (out["st_dir"] == -1)
    else:
        out["entry_long"] = out["start_long"]
        out["entry_short"] = out["start_short"]

    out["exit_long"] = out["end_long"]
    out["exit_short"] = out["end_short"]
    if exit_on_st_flip:
        out["exit_long"] = out["exit_long"] | (out["st_dir"] == -1)
        out["exit_short"] = out["exit_short"] | (out["st_dir"] == 1)
    return out
