#!/usr/bin/env python3
"""Standalone Supertrend + LDC reversal pipeline -> Supabase.

Daily operation (NO dependency on trade-jlaw-v2 or any jlaw skill):
  1. Load the shared `watchlist` table (category in core,satellite).
  2. Run the daily reversal ladder per ticker: SMA20-loss/reclaim alerts,
     trend-break / bull-stack + Supertrend confirmations, SPY + paired-ETF
     overlays, LDC exhaustion tags.
  3. Upsert the state snapshot into `trade-stldc-v2`
     (grain: run_date x symbol; merge-duplicates).
  4. For daily confirmations inside the seed window, evaluate the 2H
     entry-timing trigger variants (entry_timing_study definitions) and
     upsert into `trade-stldc-v2-events`.
     NOTE: on events, `run_date` = the daily-confirm date (the grain anchor)
     so re-runs merge; the execution time lives in `created_at`.
  5. Outcomes (ret_4/12/20) are computed where matured; events whose 20-bar
     2H horizon has not elapsed stay `pending` and backfill on later runs
     while the 60d hourly data window covers them.

Usage:
    python stldc_supabase_runner.py [--seed-days 35] [--state-only]
Creds: SUPABASE_URL / SUPABASE_KEY env vars, else ai-equity-trader-v2/.env
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import pandas_ta as ta
import requests
import yfinance as yf

from ldc_engine import LDCSettings, lorentzian_classification, supertrend
from ldc_st_runner import fetch

log = logging.getLogger("stldc_runner")

HORIZON = 20            # daily outcome horizon (context metric)
CONFIRM_WINDOW = 10     # sessions alert -> confirm
LDC_TAG_WINDOW = 5
WARMUP = 40
SEED_DAYS_DEFAULT = 35  # calendar days of daily confirms to seed events for
HORIZON_BARS = (4, 12, 20)   # 2H-bar outcome horizons
TRIGGER_SESSIONS = 10   # 2H trigger scan window (sessions) after daily confirm
WARMUP_2H = 40

TRIGGERS = ("st2h", "macd_x", "rsi_50", "sma_x", "macd_sma",
            "st_rsi", "macd_rsi60", "macd_vol", "first_any", "baseline")


# ---------------------------------------------------------------------------
# Supabase helpers (PostgREST, service key)
# ---------------------------------------------------------------------------
def load_creds() -> tuple[str, str]:
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
        return os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    env = {}
    for line in open("ai-equity-trader-v2/.env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env["SUPABASE_URL"], env["SUPABASE_KEY"]


os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")
SUPABASE_URL, SUPABASE_KEY = "", ""


def sb_get(path: str) -> list | dict:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                     headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    if not rows:
        return 0
    n = 0
    for chunk_start in range(0, len(rows), 400):
        chunk = rows[chunk_start: chunk_start + 400]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=chunk, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"upsert {table} HTTP {r.status_code}: {r.text[:300]}")
        n += len(chunk)
    return n


def clean(v):
    """JSON-safe scalar for PostgREST."""
    if v is None:
        return None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if np.isnan(f) else round(f, 6)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    return v


# ---------------------------------------------------------------------------
# Daily reversal ladder (self-contained port of reversal_strategy.py logic)
# ---------------------------------------------------------------------------
def regimes_of(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    close = df["close"]
    sma20, sma50 = ta.sma(close, 20), ta.sma(close, 50)
    return ((close < sma20) & (sma20 < sma50),
            (close > sma20) & (sma20 > sma50))


def ladder(df: pd.DataFrame, ldc: pd.DataFrame, st: pd.DataFrame,
           spy_weak: pd.Series, spy_bull: pd.Series,
           etf_weak: pd.Series, etf_bull: pd.Series, ticker: str) -> tuple[dict, list[dict]]:
    """Return (state row fields, list of (confirm_date, direction) events)."""
    close = df["close"]
    sma20, sma50 = ta.sma(close, 20), ta.sma(close, 50)
    rsi = ta.rsi(close, length=14)
    e12, e26 = ta.ema(close, 12), ta.ema(close, 26)
    if sma20 is None or sma50 is None or rsi is None or e12 is None or e26 is None:
        # listing younger than the shortest indicator - record coverage, no stage
        spy_r = "WEAK" if bool(spy_weak.iloc[-1]) else ("BULL" if bool(spy_bull.iloc[-1]) else "MIXED")
        return {
            "stage": "NONE", "stage_date": None, "stage_ago": None,
            "confirm_kind": None, "confirm_lead": None,
            "score": 0, "score_structure": 0, "score_supertrend": 0, "score_ldc": 0,
            "structure": None, "close": float(close.iloc[-1]),
            "sma20": None, "sma50": None, "rsi14": None, "macd_hist": None,
            "st_dir": None, "st_line": None,
            "ldc_signal": "NEUT", "ldc_prediction": 0,
            "spy_regime": spy_r, "ldc_short_tag": None, "ldc_long_tag": None,
        }, []
    macd = e12 - e26
    hist = macd - ta.ema(macd, 9)
    c = close.to_numpy(dtype=float)
    n = len(df)

    s20loss = (close.shift(1) >= sma20.shift(1)) & (close < sma20)
    s20recl = (close.shift(1) <= sma20.shift(1)) & (close > sma20)
    bear_stack = (close < sma20) & (sma20 < sma50)
    bull_stack = (close > sma20) & (sma20 > sma50)
    tb, bs = transitions(bear_stack), transitions(bull_stack)
    stb, sbu = st["st_flip_dn"], st["st_flip_up"]
    sig = ldc["signal"]
    ldc_s = transitions(sig == -1)
    ldc_l = transitions(sig == 1)

    sw = spy_weak.reindex(df.index).fillna(False).astype(bool).to_numpy()
    sb = spy_bull.reindex(df.index).fillna(False).astype(bool).to_numpy()
    ew = etf_weak.reindex(df.index).fillna(False).astype(bool).to_numpy()
    eb = etf_bull.reindex(df.index).fillna(False).astype(bool).to_numpy()

    # ---- current state ----
    def last_hit(s: pd.Series):
        hits = np.nonzero(s.to_numpy())[0]
        return hits[-1] if len(hits) else None

    i_ba, i_bc = last_hit(s20loss), last_hit(tb | stb)
    i_ra, i_rc = last_hit(s20recl), last_hit(bs | sbu)
    stages = {"BEAR-ALERT": i_ba, "BEAR-CONFIRM": i_bc,
              "BULL-ALERT": i_ra, "BULL-CONFIRM": i_rc}
    stage = max(stages, key=lambda k: (stages[k] if stages[k] is not None else -1))
    stage_idx = stages[stage]
    confirm_kind = None
    confirm_lead = None
    if stage == "BEAR-CONFIRM" and i_ba is not None and i_bc is not None:
        confirm_kind = "trend_break" if bool(tb.iloc[i_bc]) else "st_flip"
        confirm_lead = int(i_bc - i_ba) if i_bc > i_ba else None
    if stage == "BULL-CONFIRM" and i_ra is not None and i_rc is not None:
        confirm_kind = "trend_break" if bool(bs.iloc[i_rc]) else "st_flip"
        confirm_lead = int(i_rc - i_ra) if i_rc > i_ra else None

    struct = "BULL" if c[-1] > sma20.iloc[-1] > sma50.iloc[-1] else \
             "BEAR" if c[-1] < sma20.iloc[-1] < sma50.iloc[-1] else "FLAT"
    s_struct = 2 if struct == "BULL" else -2 if struct == "BEAR" else 0
    st_d = int(st["st_dir"].iloc[-1])
    ldc_d = int(sig.iloc[-1])
    score = s_struct + st_d + ldc_d

    ldcs_tag = within(ldc_s.to_numpy(), n - 1, LDC_TAG_WINDOW)
    ldcl_tag = within(ldc_l.to_numpy(), n - 1, LDC_TAG_WINDOW)

    state = {
        "stage": stage,
        "stage_date": df.index[stage_idx].date().isoformat() if stage_idx is not None else None,
        "stage_ago": (n - 1 - stage_idx) if stage_idx is not None else None,
        "confirm_kind": confirm_kind, "confirm_lead": confirm_lead,
        "score": score, "score_structure": s_struct,
        "score_supertrend": st_d, "score_ldc": ldc_d,
        "structure": struct,
        "close": float(c[-1]), "sma20": float(sma20.iloc[-1]), "sma50": float(sma50.iloc[-1]),
        "rsi14": float(rsi.iloc[-1]), "macd_hist": float(hist.iloc[-1]),
        "st_dir": "BULL" if st_d == 1 else "BEAR",
        "st_line": float(st["st_up" if st_d == 1 else "st_dn"].iloc[-1]),
        "ldc_signal": {1: "LONG", -1: "SHORT", 0: "NEUT"}[ldc_d],
        "ldc_prediction": int(ldc["prediction"].iloc[-1]),
        "spy_regime": "WEAK" if sw[-1] else "BULL" if sb[-1] else "MIXED",
        "ldc_short_tag": ldcs_tag, "ldc_long_tag": ldcl_tag,
    }

    # ---- confirm events inside the event window ----
    start_date = df.index[-1] - pd.Timedelta(days=EVENT_SEED_DAYS)
    events = []
    for b in range(WARMUP, n):
        d = df.index[b]
        if d < start_date:
            continue
        direction = None
        if bool(s20recl.iloc[b]) and c[b] < sma50.iloc[b]:
            conf = next((j for j in range(b + 1, min(b + 1 + CONFIRM_WINDOW, n))
                         if bool(bs.iloc[j]) or bool(sbu.iloc[j])), None)
            if conf is not None:
                direction, conf_idx = "LONG", conf
        elif bool(s20loss.iloc[b]) and c[b] > sma50.iloc[b]:
            conf = next((j for j in range(b + 1, min(b + 1 + CONFIRM_WINDOW, n))
                         if bool(tb.iloc[j]) or bool(stb.iloc[j])), None)
            if conf is not None:
                direction, conf_idx = "SHORT", conf
        if direction:
            tag_arr = ldc_s if direction == "LONG" else ldc_l
            events.append({"ticker": ticker, "confirm_idx": conf_idx,
                           "confirm_date": df.index[conf_idx], "alert_date": d,
                           "direction": direction,
                           "ldc_opposite_tag": within(tag_arr.to_numpy(), conf_idx,
                                                      LDC_TAG_WINDOW)})
    return state, events


def transitions(cond: pd.Series) -> pd.Series:
    return cond.fillna(False).astype(bool) & ~cond.shift(1).fillna(False).astype(bool)


def within(arr: np.ndarray, b: int, w: int) -> bool:
    lo = max(0, b - w + 1)
    return bool(arr[lo: b + 1].any())


def ladder_history(df: pd.DataFrame, ldc: pd.DataFrame, st: pd.DataFrame,
                   spy_weak: pd.Series, spy_bull: pd.Series, spy_close: pd.Series,
                   etf_weak: pd.Series, etf_bull: pd.Series,
                   ticker: str, category: str, min_date: pd.Timestamp) -> list[dict]:
    """Per-bar state rows (run_date = bar date), stage forward-filled as of each bar."""
    close = df["close"]
    sma20, sma50, sma200 = ta.sma(close, 20), ta.sma(close, 50), ta.sma(close, 200)
    if sma200 is None:  # pandas_ta returns None when history < length
        sma200 = pd.Series(np.nan, index=close.index)
    if sma20 is None or sma50 is None:
        return []  # listing younger than the shortest indicator
    rsi = ta.rsi(close, length=14)
    macd = ta.ema(close, 12) - ta.ema(close, 26)
    hist = macd - ta.ema(macd, 9)
    c = close.to_numpy(dtype=float)
    n = len(df)
    idx = df.index

    s20loss = (close.shift(1) >= sma20.shift(1)) & (close < sma20) & (c > sma50.to_numpy())
    s20recl = (close.shift(1) <= sma20.shift(1)) & (close > sma20) & (c < sma50.to_numpy())
    tb = transitions((close < sma20) & (sma20 < sma50)).to_numpy()
    bs = transitions((close > sma20) & (sma20 > sma50)).to_numpy()
    stb = st["st_flip_dn"].to_numpy()
    sbu = st["st_flip_up"].to_numpy()
    bc = tb | stb
    rc = bs | sbu
    sig = ldc["signal"].to_numpy()
    pred = ldc["prediction"].to_numpy()
    st_dir = st["st_dir"].to_numpy()
    st_line = np.where(st_dir == 1, st["st_up"].to_numpy(), st["st_dn"].to_numpy())
    ldc_s = transitions(ldc["signal"] == -1).to_numpy()
    ldc_l = transitions(ldc["signal"] == 1).to_numpy()
    sw = spy_weak.reindex(idx).fillna(False).astype(bool).to_numpy()
    sb = spy_bull.reindex(idx).fillna(False).astype(bool).to_numpy()
    sc = spy_close.reindex(idx).to_numpy()
    ew = etf_weak.reindex(idx).fillna(False).astype(bool).to_numpy()
    eb = etf_bull.reindex(idx).fillna(False).astype(bool).to_numpy()

    rows = []
    stage, stage_idx, kind, lead = "NONE", None, None, None
    last_ba = last_ra = None
    struct_s = np.where((c > sma20) & (sma20 > sma50), "BULL",
                        np.where((c < sma20) & (sma20 < sma50), "BEAR", "FLAT"))
    s_struct = np.where(struct_s == "BULL", 2, np.where(struct_s == "BEAR", -2, 0))
    for b in range(WARMUP, n):
        if s20loss[b]:
            stage, stage_idx, kind, lead, last_ba = "BEAR-ALERT", b, None, None, b
        if bc[b]:
            stage, stage_idx = "BEAR-CONFIRM", b
            kind = "trend_break" if tb[b] else "st_flip"
            lead = (b - last_ba) if last_ba is not None else None
        if s20recl[b]:
            stage, stage_idx, kind, lead, last_ra = "BULL-ALERT", b, None, None, b
        if rc[b]:
            stage, stage_idx = "BULL-CONFIRM", b
            kind = "trend_break" if bs[b] else "st_flip"
            lead = (b - last_ra) if last_ra is not None else None
        if idx[b] < min_date:
            continue
        spy_r = "WEAK" if sw[b] else "BULL" if sb[b] else "MIXED"
        rows.append({
            "run_date": idx[b].date().isoformat(), "last_bar_ts": idx[b].isoformat(),
            "symbol": ticker, "category": category,
            "stage": stage,
            "stage_date": idx[stage_idx].date().isoformat() if stage_idx is not None else None,
            "stage_ago": (b - stage_idx) if stage_idx is not None else None,
            "confirm_kind": kind, "confirm_lead": lead,
            "score": int(s_struct[b]) + int(st_dir[b]) + int(sig[b]),
            "score_structure": int(s_struct[b]),
            "score_supertrend": int(st_dir[b]), "score_ldc": int(sig[b]),
            "structure": struct_s[b], "close": c[b],
            "sma20": sma20.iloc[b], "sma50": sma50.iloc[b], "sma200": sma200.iloc[b],
            "rsi14": rsi.iloc[b], "macd_hist": hist.iloc[b],
            "st_dir": "BULL" if st_dir[b] == 1 else "BEAR", "st_line": st_line[b],
            "ldc_signal": {1: "LONG", -1: "SHORT", 0: "NEUT"}[int(sig[b])],
            "ldc_prediction": int(pred[b]),
            "spy_regime": spy_r, "spy_close": sc[b],
            "ldc_short_tag": within(ldc_s, b, LDC_TAG_WINDOW),
            "ldc_long_tag": within(ldc_l, b, LDC_TAG_WINDOW),
        })
    return rows


# ---------------------------------------------------------------------------
# 2H entry-timing triggers (entry_timing_study definitions)
# ---------------------------------------------------------------------------
def resample_2h(hourly: pd.DataFrame) -> pd.DataFrame:
    return (hourly.resample("2h", label="left", closed="left")
                  .agg({"open": "first", "high": "max", "low": "min",
                        "close": "last", "volume": "sum"})
                  .dropna())


def two_h_frame(df_daily: pd.DataFrame) -> pd.DataFrame | None:
    hourly = yf.download(df_daily.attrs.get("ticker", ""), period="730d", interval="1h",
                         progress=False, auto_adjust=True, threads=False)
    if hourly is None or hourly.empty:
        return None
    if isinstance(hourly.columns, pd.MultiIndex):
        hourly.columns = hourly.columns.get_level_values(0)
    hourly = hourly[["Open", "High", "Low", "Close", "Volume"]].dropna()
    hourly = hourly.rename(columns=str.lower)
    th = resample_2h(hourly)
    if len(th) < WARMUP_2H + 10:
        return None
    st_in = th.copy()
    t_st = supertrend(st_in)
    th["sma20"] = ta.sma(th["close"], 20)
    th["rsi"] = ta.rsi(th["close"], length=14)
    macd = ta.ema(th["close"], 12) - ta.ema(th["close"], 26)
    th["macd"], th["sig"] = macd, ta.ema(macd, 9)
    th["st_dir"] = t_st["st_dir"].to_numpy()
    th["stb"] = t_st["st_flip_dn"].to_numpy()
    th["sbu"] = t_st["st_flip_up"].to_numpy()
    th["volx"] = th["volume"] / th["volume"].rolling(20).mean()
    th["macd_x_up"] = (th["macd"] > th["sig"]) & (th["macd"].shift(1) <= th["sig"].shift(1))
    th["macd_x_dn"] = (th["macd"] < th["sig"]) & (th["macd"].shift(1) >= th["sig"].shift(1))
    th["rsi_x_up"] = (th["rsi"] > 50) & (th["rsi"].shift(1) <= 50)
    th["rsi_x_dn"] = (th["rsi"] < 50) & (th["rsi"].shift(1) >= 50)
    th["sma_x_up"] = (th["close"] > th["sma20"]) & (th["close"].shift(1) <= th["sma20"].shift(1))
    th["sma_x_dn"] = (th["close"] < th["sma20"]) & (th["close"].shift(1) >= th["sma20"].shift(1))
    return th.dropna(subset=["close", "sma20", "rsi", "macd", "sig"])


def trigger_events(df_daily: pd.DataFrame, th: pd.DataFrame, ev: dict) -> list[dict]:
    """Rows for one daily confirm event: one per trigger variant + baseline."""
    c = df_daily["close"].to_numpy(dtype=float)
    idx_d = df_daily.index
    conf_bar = int(idx_d.get_loc(ev["confirm_date"]))
    direction = 1 if ev["direction"] == "LONG" else -1
    end_d = idx_d[min(conf_bar + TRIGGER_SESSIONS, len(idx_d) - 1)].date()
    d = ev["confirm_date"].date()

    th_idx = th.index
    th_n = len(th)
    w0 = next((k for k in range(WARMUP_2H, th_n) if th_idx[k].date() > d), None)
    if w0 is None:
        return []
    w1 = next((k for k in range(th_n - 1, w0 - 1, -1) if th_idx[k].date() <= end_d), None)
    if w1 is None or w1 <= w0:
        return []
    win = th.iloc[w0: w1 + 1]
    up = direction == 1

    masks = {
        "st2h": (win["sbu"] if up else win["stb"]).to_numpy(),
        "macd_x": (win["macd_x_up"] if up else win["macd_x_dn"]).to_numpy(),
        "rsi_50": (win["rsi_x_up"] if up else win["rsi_x_dn"]).to_numpy(),
        "sma_x": (win["sma_x_up"] if up else win["sma_x_dn"]).to_numpy(),
        "macd_sma": ((win["macd"] > win["sig"]) if up else (win["macd"] < win["sig"])).to_numpy()
                    & ((win["close"] > win["sma20"]) if up else (win["close"] < win["sma20"])).to_numpy(),
        "st_rsi": ((win["st_dir"] == 1) if up else (win["st_dir"] == -1)).to_numpy()
                  & ((win["rsi"] > 50) if up else (win["rsi"] < 50)).to_numpy(),
        "macd_rsi60": (win["macd_x_up"] if up else win["macd_x_dn"]).to_numpy()
                      & ((win["rsi"] >= 40) & (win["rsi"] <= 60)).to_numpy(),
        "macd_vol": (win["macd_x_up"] if up else win["macd_x_dn"]).to_numpy()
                    & (win["volx"] >= 1.2).to_numpy(),
        "first_any": None,
        "baseline": np.ones(len(win), dtype=bool),
    }
    masks["first_any"] = (masks["st2h"] | masks["macd_x"] | masks["rsi_50"] | masks["sma_x"])

    def entry_for(mask: np.ndarray):
        hits = np.nonzero(mask)[0]
        if not len(hits):
            return None
        b = int(hits[0])
        k_entry = w0 + b + 1
        if k_entry >= th_n:
            return None
        entry_px = float(th["open"].iloc[k_entry])
        outs = {}
        for hb in HORIZON_BARS:
            k = k_entry + hb
            outs[hb] = (float(th["close"].iloc[k]) / entry_px - 1.0) * direction \
                if k < th_n else None
        return {"trigger_ts": th_idx[k_entry - 1], "entry_ts": th_idx[k_entry],
                "entry_price": entry_px, **{f"ret_{h}": outs[h] for h in HORIZON_BARS},
                "matured": all(k_entry + h < th_n for h in HORIZON_BARS)}

    base = entry_for(masks["baseline"])
    baseline_price = base["entry_price"] if base else None
    rows = []
    for name in TRIGGERS:
        rec = base if name == "baseline" else entry_for(masks[name])
        row = {
            "symbol": df_daily.attrs.get("ticker", ""),
            "run_date": ev["confirm_date"].date().isoformat(),
            "direction": ev["direction"],
            "daily_stage": "BULL-CONFIRM" if direction == 1 else "BEAR-CONFIRM",
            "daily_confirm_date": ev["confirm_date"].date().isoformat(),
            "trigger_kind": name,
            "trigger_ts": rec["trigger_ts"].isoformat() if rec else
                          f"{end_d.isoformat()}T23:59:00",
            "entry_ts": rec["entry_ts"].isoformat() if rec else None,
            "entry_price": rec["entry_price"] if rec else None,
            "baseline_price": baseline_price,
            "non_extended": None, "ldc_opposite_tag": bool(ev.get("ldc_opposite_tag", False)),
            "outcome_status": ("matured" if rec and rec["matured"] else "pending"),
            "ret_4": rec["ret_4"] if rec else None,
            "ret_12": rec["ret_12"] if rec else None,
            "ret_20": rec["ret_20"] if rec else None,
            "outcome_ts": (rec["entry_ts"] + pd.Timedelta(hours=40)).isoformat()
                          if rec and rec["matured"] else None,
        }
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
EVENT_SEED_DAYS = 35
os.environ.setdefault("SEED_DAYS", str(SEED_DAYS_DEFAULT))


def main() -> int:
    global SUPABASE_URL, SUPABASE_KEY
    ap = argparse.ArgumentParser(description="Standalone Supertrend+LDC -> Supabase runner")
    ap.add_argument("--seed-days", type=int, default=35,
                    help="calendar days of daily confirms to seed 2H events for")
    ap.add_argument("--state-history", type=int, default=0,
                    help="calendar days of per-bar state history to backfill (0 = off)")
    ap.add_argument("--state-only", action="store_true", help="skip the 2H events layer")
    args = ap.parse_args()
    global EVENT_SEED_DAYS
    EVENT_SEED_DAYS = args.seed_days

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    SUPABASE_URL, SUPABASE_KEY = load_creds()

    # ---- watchlist (shared table; no jlaw dependency) ----
    wl = sb_get("watchlist?select=symbol,category"
                "&category=in.(core,satellite,watch1,watch2,watch3)&limit=1000")
    cat_map = {r["symbol"]: r["category"] for r in wl}
    tickers = sorted(cat_map)
    log.info("Watchlist: %d tickers", len(tickers))

    pairs: dict[str, tuple[str, float]] = {}
    try:
        p = pd.read_csv("out/watchlist_etf_pairs.csv")
        pairs = {r.symbol: (r.etf, float(r.corr_1y)) for r in p.itertuples()}
    except FileNotFoundError:
        log.warning("watchlist_etf_pairs.csv not found - SPY used as context for all")

    data: dict[str, pd.DataFrame] = {}
    states, all_events = [], []
    run_date = None
    for ticker in tickers:
        try:
            df = fetch(ticker, "5y", "1d")
        except Exception as exc:
            log.error("%s: fetch failed (%s)", ticker, exc)
            continue
        df.attrs["ticker"] = ticker
        data[ticker] = df
        run_date = max(run_date, df.index[-1]) if run_date else df.index[-1]
    log.info("Daily data for %d tickers; run_date=%s", len(data), run_date.date())

    spy_weak, spy_bull = regimes_of(data["SPY"]) if "SPY" in data else (None, None)
    for ticker, df in data.items():
        try:
            ldc = lorentzian_classification(df, LDCSettings())
            st = supertrend(df)
            pair_etf, pair_corr = pairs.get(ticker, ("SPY", 0.0))
            use_pair = pair_corr >= 0.5 and pair_etf in data and pair_etf != ticker
            src = pair_etf if use_pair else "SPY"
            if src in data:
                etf_weak, etf_bull = regimes_of(data[src])
            else:
                etf_weak = etf_bull = pd.Series(False, index=df.index)
            spy_w = spy_weak.reindex(df.index).fillna(False).astype(bool) if spy_weak is not None \
                else pd.Series(False, index=df.index)
            spy_b = spy_bull.reindex(df.index).fillna(False).astype(bool) if spy_bull is not None \
                else pd.Series(False, index=df.index)

            state, events = ladder(df, ldc, st, spy_w, spy_b, etf_weak, etf_bull, ticker)
            state.update({
                "run_date": run_date.date().isoformat(),
                "last_bar_ts": df.index[-1].isoformat(),
                "symbol": ticker, "category": cat_map.get(ticker, "?"),
                "spy_close": float(data["SPY"]["close"].iloc[-1]) if "SPY" in data else None,
                "pair_etf": src, "pair_etf_corr": round(pair_corr, 3),
            })
            reg = "WEAK" if bool(etf_weak.iloc[-1]) else "BULL" if bool(etf_bull.iloc[-1]) else "MIXED"
            state["pair_etf_regime"] = reg
            spy_close = data["SPY"]["close"].iloc[-1] if "SPY" in data else None
            spy_sma20 = float(ta.sma(data["SPY"]["close"], 20).iloc[-1]) if "SPY" in data else None
            spy_sma50 = float(ta.sma(data["SPY"]["close"], 50).iloc[-1]) if "SPY" in data else None
            state["spy_regime"] = ("WEAK" if spy_close < spy_sma20 < spy_sma50
                                   else "BULL" if spy_close > spy_sma20 > spy_sma50 else "MIXED")
            states.append(state)
            for ev in events:
                ev["ldc_opposite_tag"] = False
            all_events.extend(events)
        except Exception as exc:
            log.error("%s: ladder failed (%s)", ticker, exc)

    n_state = sb_upsert("trade-stldc-v2", [{k: clean(v) for k, v in s.items()} for s in states],
                        on_conflict="run_date,symbol")
    log.info("Upserted %d state rows -> trade-stldc-v2 (run_date %s)",
             n_state, run_date.date())

    # ---- per-bar state history backfill ----
    if args.state_history > 0:
        min_date = run_date - pd.Timedelta(days=args.state_history)
        hist_rows: list[dict] = []
        for ticker, df in data.items():
            try:
                ldc_h = lorentzian_classification(df, LDCSettings())
                st_h = supertrend(df)
                pair_etf, pair_corr = pairs.get(ticker, ("SPY", 0.0))
                use_pair = pair_corr >= 0.5 and pair_etf in data and pair_etf != ticker
                src = pair_etf if use_pair else "SPY"
                if src in data:
                    etf_weak_h, etf_bull_h = regimes_of(data[src])
                else:
                    etf_weak_h = etf_bull_h = pd.Series(False, index=df.index)
                spy_w_h = spy_weak.reindex(df.index).fillna(False).astype(bool) \
                    if spy_weak is not None else pd.Series(False, index=df.index)
                spy_b_h = spy_bull.reindex(df.index).fillna(False).astype(bool) \
                    if spy_bull is not None else pd.Series(False, index=df.index)
                spy_close_s = data["SPY"]["close"] if "SPY" in data else pd.Series(np.nan, index=df.index)
                hist_rows.extend(ladder_history(
                    df, ldc_h, st_h, spy_w_h, spy_b_h, spy_close_s,
                    etf_weak_h, etf_bull_h, ticker, cat_map.get(ticker, "?"), min_date))
            except Exception as exc:
                log.error("%s: state history failed (%s)", ticker, exc)
        n_hist = sb_upsert("trade-stldc-v2",
                           [{k: clean(v) for k, v in r.items()} for r in hist_rows],
                           on_conflict="run_date,symbol")
        log.info("Upserted %d state-history rows (since %s)", n_hist, min_date.date())

    if args.state_only or not all_events:
        log.info("Done (state-only=%s, %d confirm events).", args.state_only, len(all_events))
        return 0

    # ---- 2H events ----
    seed_start = run_date - pd.Timedelta(days=EVENT_SEED_DAYS)
    evs = [e for e in all_events if e["confirm_date"] >= seed_start]
    log.info("Seeding 2H events for %d daily confirms since %s", len(evs), seed_start.date())

    existing = sb_get("trade-stldc-v2-events"
                      "?select=run_date,symbol,direction,trigger_kind,trigger_ts"
                      f"&run_date=gte.{seed_start.date()}")
    seen = {(r["run_date"], r["symbol"], r["direction"], r["trigger_kind"], r["trigger_ts"])
            for r in existing} if isinstance(existing, list) else set()

    event_rows: list[dict] = []
    two_h_cache: dict[str, pd.DataFrame] = {}
    symbols_done = set()
    for ticker in data:
        evs_t = [e for e in evs if e["ticker"] == ticker]
        if not evs_t:
            continue
        try:
            if ticker not in two_h_cache:
                two_h_cache[ticker] = two_h_frame(data[ticker])
            th = two_h_cache[ticker]
            if th is None:
                log.warning("%s: no usable 2H series - events skipped", ticker)
                continue
            for ev in evs_t:
                for row in trigger_events(data[ticker], th, ev):
                    key = (row["run_date"], row["symbol"], row["direction"],
                           row["trigger_kind"], row["trigger_ts"])
                    if key in seen:
                        continue
                    seen.add(key)
                    event_rows.append(row)
            symbols_done.add(ticker)
            time.sleep(0.1)
        except Exception as exc:
            log.error("%s: 2H event evaluation failed (%s)", ticker, exc)

    n_ev = sb_upsert("trade-stldc-v2-events",
                     [{k: clean(v) for k, v in r.items()} for r in event_rows],
                     on_conflict="run_date,symbol,direction,trigger_kind,trigger_ts")
    matured = sum(1 for r in event_rows if r["outcome_status"] == "matured")
    log.info("Upserted %d event rows (%d matured) from %d symbols -> trade-stldc-v2-events",
             n_ev, matured, len(symbols_done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
