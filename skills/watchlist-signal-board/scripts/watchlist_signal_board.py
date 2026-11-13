#!/usr/bin/env python3
# ABOUTME: Watchlist-wide signal consensus board — fresh technical scan vs upstream signals.
# ABOUTME: Pulls jlaw-v2 (yahoo+chart), flow verdicts, quant2, stldc from Supabase; self-contained, stdlib+yfinance+pandas+requests.

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

_NY = ZoneInfo("America/New_York")
WATCHLIST_CATEGORIES = ["core", "satellite", "watch1", "watch2", "watch3"]
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "ARCA", "BATS", "CBOE"}
_FRESH_DAYS = 10  # crossover freshness window (bars)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_creds(args) -> tuple[str, str]:
    """SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY env vars, or --creds file fallback."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return url.rstrip("/").removesuffix("/rest/v1"), key
    if args.creds and Path(args.creds).exists():
        raw = Path(args.creds).read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")

        def grab(k):
            for line in raw.split("\n"):
                if line.strip().startswith(k + "="):
                    return line.split("=", 1)[1].strip()
            return None

        f_url, f_key = grab("supabase_url"), grab("supabase_service_role")
        if f_url and f_key:
            return f_url.rstrip("/").removesuffix("/rest/v1"), f_key
    raise SystemExit(
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set (env), or pass --creds <path> "
        "with a file containing supabase_url= and supabase_service_role= lines."
    )


class SB:
    """Minimal PostgREST client (never logs the key)."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/")
        if not self.base.endswith("/rest/v1"):
            self.base += "/rest/v1"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}

    def select(self, table: str, params: dict | None = None):
        r = requests.get(f"{self.base}/{table}", headers=self.h, params=params, timeout=30)
        if not r.ok:
            raise RuntimeError(f"{table}: HTTP {r.status_code}: {r.text[:120]}")
        return r.json()


# ---------------------------------------------------------------------------
# Technical indicators (pandas ports matching pandas-ta defaults)
# ---------------------------------------------------------------------------


def _ema(series: pd.Series, length: int) -> pd.Series:
    """pandas-ta ema (presma=True): seed index length-1 with the SMA of the
    first `length` values, then recursive span-ewm (adjust=False)."""
    if len(series) < length:
        return pd.Series(float("nan"), index=series.index)
    seeded = series.copy()
    sma_nth = seeded.iloc[0:length].mean()  # NaN when the head contains NaNs
    seeded.iloc[: length - 1] = float("nan")
    seeded.iloc[length - 1] = sma_nth
    return seeded.ewm(span=length, adjust=False).mean()


def _rma(series: pd.Series, length: int) -> pd.Series:
    """pandas-ta rma (Wilder): bare ewm(alpha=1/length, adjust=False) — no seed,
    recursion starts at the first valid value."""
    return series.ewm(alpha=1 / length, adjust=False).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """pandas-ta rsi: scalar * pos_avg / (pos_avg + |neg_avg|), both RMA-smoothed."""
    delta = close.diff()
    gain = delta.copy()
    gain[gain < 0] = 0.0
    loss = delta.copy()
    loss[loss > 0] = 0.0
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length).abs()
    return 100 * avg_gain / (avg_gain + avg_loss)


def _adx(df: pd.DataFrame, length: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder ADX with +DI/-DI — pandas-ta adx non-talib path, replicating its
    head handling: DM row-0 NaN -> 0 (zero() helper), and atr(prenan=True)
    seeds TR with the SMA of the first `length` values so dmp/dmn/dx carry a
    NaN head and the ADX recursion starts at the first valid DX."""
    high, low, close = df["High"], df["Low"], df["Close"]
    up = high.diff()
    dn = low.shift(1) - low
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    plus_dm = plus_dm.fillna(0.0)
    minus_dm = minus_dm.fillna(0.0)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    tr_s = tr.copy()
    tr_s.iloc[: length - 1] = float("nan")
    tr_s.iloc[length - 1] = tr.iloc[:length].mean()
    atr_ = tr_s.ewm(alpha=1 / length, adjust=False).mean()
    dmp = 100 * _rma(plus_dm, length) / atr_
    dmn = 100 * _rma(minus_dm, length) / atr_
    dx = 100 * (dmp - dmn).abs() / (dmp + dmn)
    adx = _rma(dx, length)
    return adx, dmp, dmn


def _macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    line = _ema(close, fast) - _ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()  # NaN head: recursion starts at first valid
    hist = line - sig
    return pd.DataFrame({"line": line, "signal": sig, "hist": hist})


def _last_crossing(diff: pd.Series) -> dict | None:
    """Most recent sign change of a (fast-slow) diff series -> direction + days_ago."""
    values = diff.dropna().to_numpy()
    if len(values) < 2:
        return None
    for i in range(len(values) - 1, 0, -1):
        curr, prev = values[i], values[i - 1]
        if curr > 0 and prev <= 0:
            return {"direction": "up", "days_ago": len(values) - 1 - i}
        if curr < 0 and prev >= 0:
            return {"direction": "down", "days_ago": len(values) - 1 - i}
    return None


def compute_raw_indicators(df: pd.DataFrame) -> dict:
    """Raw indicator values from an OHLCV frame (same keys as the source module)."""
    result = {
        "rsi": None, "sma20": None, "sma50": None,
        "macd_line": None, "macd_signal": None, "macd_hist": None, "prev_macd_hist": None,
        "macd_crossover": None, "ema9": None, "ema21": None, "ema_crossover": None,
        "adx": None, "dmp": None, "dmn": None,
    }
    if df.empty or "Close" not in df.columns:
        return result

    close = df["Close"]
    rsi = _rsi(close)
    if len(rsi) and pd.notna(rsi.iloc[-1]):
        result["rsi"] = float(rsi.iloc[-1])

    for key, n in (("sma20", 20), ("sma50", 50)):
        sma = close.rolling(n).mean()
        if pd.notna(sma.iloc[-1]):
            result[key] = float(sma.iloc[-1])

    ema9 = _ema(close, 9)
    ema21 = _ema(close, 21)
    if pd.notna(ema9.iloc[-1]):
        result["ema9"] = float(ema9.iloc[-1])
    if pd.notna(ema21.iloc[-1]):
        result["ema21"] = float(ema21.iloc[-1])
    result["ema_crossover"] = _last_crossing(ema9 - ema21)

    macd = _macd(close)
    if len(macd) and pd.notna(macd["line"].iloc[-1]):
        result["macd_line"] = float(macd["line"].iloc[-1])
        result["macd_signal"] = float(macd["signal"].iloc[-1])
        result["macd_hist"] = float(macd["hist"].iloc[-1])
        if len(macd) > 1 and pd.notna(macd["hist"].iloc[-2]):
            result["prev_macd_hist"] = float(macd["hist"].iloc[-2])
        result["macd_crossover"] = _last_crossing(macd["hist"])

    if "High" in df.columns and "Low" in df.columns:
        adx, dmp, dmn = _adx(df)
        if pd.notna(adx.iloc[-1]):
            result["adx"] = float(adx.iloc[-1])
        if pd.notna(dmp.iloc[-1]):
            result["dmp"] = float(dmp.iloc[-1])
        if pd.notna(dmn.iloc[-1]):
            result["dmn"] = float(dmn.iloc[-1])

    return result


def get_earnings_info_light(symbol: str) -> dict:
    """Best-effort next earnings date + BMO/AMC timing from yfinance."""
    out = {"earnings_date": None, "timing": None}
    try:
        ed = yf.Ticker(symbol).earnings_dates
        if ed is None or ed.empty:
            return out
        now = pd.Timestamp.now(tz="America/New_York")
        future = ed[ed.index >= now]
        if future.empty:
            return out
        idx = future.index[0]
        out["earnings_date"] = str(idx.date())
        if "EPS Report Timing" in future.columns:
            timing = future.iloc[0]["EPS Report Timing"]
            if isinstance(timing, str):
                out["timing"] = timing
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Fresh technical scan (bullish composite, /9.5)
# ---------------------------------------------------------------------------


def _score_dual_crossover(ema_x: dict | None, macd_x: dict | None) -> float:
    if not ema_x or not macd_x:
        return 0.0
    if ema_x["direction"] != macd_x["direction"]:
        return -0.5
    both_fresh = ema_x["days_ago"] <= _FRESH_DAYS and macd_x["days_ago"] <= _FRESH_DAYS
    if ema_x["direction"] == "up":
        return 1.0 if both_fresh else 0.5
    return -1.0 if both_fresh else -0.5


def compute_bullish_score(symbol: str, period: str = "3mo") -> dict | None:
    """Composite bullish trend score (same rubric as scanner-bullish, /~9.5)."""
    try:
        df = yf.Ticker(symbol).history(period=period).dropna(subset=["Close"])
        if df.empty or len(df) < 50:
            return None

        score, signals = 0.0, []
        price = float(df["Close"].iloc[-1])
        period_return = (price / float(df["Close"].iloc[0]) - 1) * 100
        raw = compute_raw_indicators(df)

        pct_sma20 = pct_sma50 = 0
        if raw["sma20"] is not None:
            pct_sma20 = ((price - raw["sma20"]) / raw["sma20"]) * 100
            if price > raw["sma20"]:
                score += 1.0
        if raw["sma50"] is not None:
            pct_sma50 = ((price - raw["sma50"]) / raw["sma50"]) * 100
            if price > raw["sma50"]:
                score += 1.0

        rsi = raw["rsi"]
        if rsi is not None:
            if 50 <= rsi <= 70:
                score += 1.0
            elif 30 <= rsi < 50:
                score += 0.5
            elif rsi < 30:
                score += 0.25

        if raw["macd_line"] is not None and raw["macd_signal"] is not None:
            if raw["macd_line"] > raw["macd_signal"]:
                score += 1.0
        if raw["macd_hist"] is not None and raw["prev_macd_hist"] is not None:
            if raw["macd_hist"] > raw["prev_macd_hist"]:
                score += 0.5

        if raw["ema9"] is not None and raw["ema21"] is not None:
            score += 0.5 if raw["ema9"] > raw["ema21"] else -0.25

        score += _score_dual_crossover(raw["ema_crossover"], raw["macd_crossover"])

        if None not in (raw["adx"], raw["dmp"], raw["dmn"]):
            if raw["adx"] > 25 and raw["dmp"] > raw["dmn"]:
                score += 1.5
            elif raw["dmp"] > raw["dmn"]:
                score += 0.5

        score += min(max(period_return / 20, -1.0), 2.0)

        einfo = get_earnings_info_light(symbol)
        return {
            "symbol": symbol,
            "score": round(score, 2),
            "price": round(price, 2),
            "next_earnings": einfo["earnings_date"],
            "earnings_timing": einfo["timing"],
            "period_return_pct": round(period_return, 2),
            "pct_from_sma20": round(pct_sma20, 2),
            "pct_from_sma50": round(pct_sma50, 2),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "adx": round(raw["adx"], 2) if raw["adx"] is not None else None,
            "macd_crossover": raw["macd_crossover"],
            "ema_crossover": raw["ema_crossover"],
        }
    except Exception as e:
        print(f"  scan error {symbol}: {e}", file=sys.stderr)
        return None


def scan_symbols(symbols: list[str], period: str = "3mo", workers: int = 10) -> dict:
    by_sym = {}
    total = len(symbols)
    print(f"Scanning {total} symbols ({period})...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(compute_bullish_score, s, period): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            sym = futs[fut]
            try:
                r = fut.result()
                if r:
                    by_sym[sym.upper()] = r
            except Exception as e:
                print(f"  failed {sym}: {e}", file=sys.stderr)
            if i % 50 == 0:
                print(f"  {i}/{total}", file=sys.stderr)
    print(f"Scan done: {len(by_sym)} scored in {time.time() - _T0:.0f}s", file=sys.stderr)
    return by_sym


_T0 = time.time()

# ---------------------------------------------------------------------------
# Upstream signal fetchers
# ---------------------------------------------------------------------------


def fetch_jlaw(sb: SB) -> tuple[dict, dict, str, str]:
    out, dates = {}, {}
    for rtype in ("yahoo", "chart"):
        latest = sb.select(
            "trade-jlaw-v2",
            {"select": "run_date", "jlaw_review_type": f"eq.{rtype}", "order": "run_date.desc", "limit": "1"},
        )
        if not latest:
            continue
        run_date = latest[0]["run_date"]
        dates[rtype] = run_date
        rows = sb.select(
            "trade-jlaw-v2",
            {
                "select": "symbol,jlaw_score,classification,current_price,stop_loss,"
                "target_price,chase_pct,rrr,vcp_stage,pocket_pivot,regime",
                "jlaw_review_type": f"eq.{rtype}",
                "run_date": f"eq.{run_date}",
            },
        )
        m = {}
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            if sym and sym not in m:
                m[sym] = r
        out[rtype] = m
    return out.get("yahoo", {}), out.get("chart", {}), dates.get("yahoo", ""), dates.get("chart", "")


def fetch_stldc(sb: SB) -> tuple[dict, str]:
    latest = sb.select("trade-stldc-v2", {"select": "run_date", "order": "run_date.desc", "limit": "1"})
    if not latest:
        return {}, ""
    run_date = latest[0]["run_date"]
    rows = sb.select(
        "trade-stldc-v2",
        {
            "select": "symbol,st_dir,ldc_signal,ldc_prediction,structure,score,score_ldc,"
            "score_structure,score_supertrend,spy_regime,rsi14,stage,detail",
            "run_date": f"eq.{run_date}",
        },
    )
    return {(r.get("symbol") or "").upper(): r for r in rows}, run_date


def fetch_quant2(sb: SB) -> tuple[dict, str]:
    latest = sb.select("quant2_leaderboard", {"select": "run_name", "order": "created_at.desc", "limit": "1"})
    if not latest:
        return {}, ""
    run = latest[0]["run_name"]
    rows = sb.select(
        "quant2_leaderboard",
        {"select": "ticker,rank,grp,n_active_rules,rsi14,as_of", "run_name": f"eq.{run}"},
    )
    return {(r.get("ticker") or "").upper(): r for r in rows}, run


def fetch_flow(sb: SB) -> tuple[dict, str]:
    latest = sb.select("flow_verdict_rows", {"select": "verdict_date", "order": "verdict_date.desc", "limit": "1"})
    if not latest:
        return {}, ""
    vd = latest[0]["verdict_date"]
    rows = sb.select(
        "flow_verdict_rows",
        {
            "select": "symbol,verdict,verdict_date,premium_tilt,bull_premium,bear_premium,"
            "alert_count,tide_bias,next_earnings_date",
            "verdict_date": f"eq.{vd}",
        },
    )
    return {(r.get("symbol") or "").upper(): r for r in rows}, str(vd)[:10]


# ---------------------------------------------------------------------------
# Supabase destination upsert (idempotent on signal_key = run_date|symbol)
# ---------------------------------------------------------------------------

TABLE = "watchlist-signal-board"


def _num(v, nd=None):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if nd is not None else f


def _date10(v):
    return str(v)[:10] if v else None


def build_rows(board, scan_by_sym, meta, run_date):
    """Map board rows to the destination schema (sql/watchlist_signal_board.sql)."""
    rows = []
    for r in board:
        sym = r["symbol"]
        s = scan_by_sym.get(sym) or {}
        scan_signals = {
            "period_return_pct": s.get("period_return_pct"),
            "pct_from_sma20": s.get("pct_from_sma20"),
            "pct_from_sma50": s.get("pct_from_sma50"),
            "macd_crossover": s.get("macd_crossover"),
            "ema_crossover": s.get("ema_crossover"),
            "earnings_timing": s.get("earnings_timing"),
        }
        rd = meta.get("run_dates", {})
        rows.append(
            {
                "signal_key": f"{run_date}|{sym}",
                "run_date": run_date,
                "symbol": sym,
                "category": r.get("category"),
                "scan_score": _num(r.get("scan_score"), 2),
                "scan_rsi": _num(r.get("scan_rsi"), 2),
                "scan_adx": _num(r.get("scan_adx"), 2),
                "scan_price": _num(r.get("scan_price"), 4),
                "scan_period": meta.get("scan_period", "3mo"),
                "scan_missing": bool(r.get("scan_missing")),
                "next_earnings": _date10(r.get("next_earnings")),
                "scan_signals": scan_signals,
                "jlaw_yahoo_score": r.get("jlaw_yahoo_score"),
                "jlaw_yahoo_cls": r.get("jlaw_yahoo_cls"),
                "jlaw_yahoo_stop": _num(r.get("jlaw_yahoo_stop"), 4),
                "jlaw_yahoo_target": _num(r.get("jlaw_yahoo_target"), 4),
                "jlaw_yahoo_run_date": _date10(rd.get("jlaw_yahoo")),
                "jlaw_chart_cls": r.get("jlaw_chart_cls"),
                "jlaw_chart_vcp": r.get("jlaw_chart_vcp"),
                "jlaw_chart_run_date": _date10(rd.get("jlaw_chart")),
                "flow_verdict": r.get("flow_verdict"),
                "flow_tilt": _num(r.get("flow_tilt"), 2),
                "flow_run_date": _date10(rd.get("flow_verdict")),
                "flow_stale": r.get("flow_stale"),
                "quant2_rank": r.get("quant2_rank"),
                "quant2_rules": r.get("quant2_rules"),
                "quant2_grp": r.get("quant2_grp"),
                "quant2_run": rd.get("quant2"),
                "stldc_signal": r.get("stldc_signal"),
                "stldc_dir": r.get("stldc_dir"),
                "stldc_score": r.get("stldc_score"),
                "stldc_structure": r.get("stldc_structure"),
                "stldc_run_date": _date10(rd.get("stldc")),
                "votes_bull": r["votes"]["bull"],
                "votes_bear": r["votes"]["bear"],
                "votes_neut": r["votes"]["neut"],
                "consensus": r.get("consensus"),
                "conflict": bool(r.get("conflict")),
                "chart_veto": bool(r.get("chart_veto")),
                "directions": r.get("directions"),
                "spy_regime": meta.get("spy_regime"),
                "meta": meta,
            }
        )
    return rows


def upsert_rows(sb: SB, rows: list[dict]) -> int:
    """Batch upsert via PostgREST (merge-duplicates on signal_key)."""
    written = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        r = requests.post(
            f"{sb.base}/{TABLE}",
            headers={**sb.h, "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "signal_key"},
            json=chunk,
            timeout=60,
        )
        if not r.ok:
            raise RuntimeError(f"upsert {TABLE}: HTTP {r.status_code}: {r.text[:200]}")
        written += len(chunk)
    return written


# ---------------------------------------------------------------------------
# Direction mapping (see references/methodology.md)
# ---------------------------------------------------------------------------


def scan_direction(score):
    if score is None:
        return "—"
    if score >= 4:
        return "BULL"
    if score <= 2:
        return "BEAR"
    return "NEUT"


def jlaw_yahoo_direction(row):
    if not row:
        return "—"
    score = row.get("jlaw_score") or 0
    if row.get("classification") == "Watch" or score >= 8:
        return "BULL"  # setup forming (Watch/Valid are the actionable bands)
    return "NEUT"  # Skip = no entry setup, not a directional bet


def jlaw_chart_direction(row):
    if not row:
        return "—"
    cls = row.get("classification")
    if cls == "Watch":
        return "BULL"
    if cls == "Skip":
        return "NEUT"  # chart veto — surfaced as an override, not a bear vote
    return "—"


def flow_direction(row, today):
    if not row:
        return "—", False
    verdict = (row.get("verdict") or "no-data").lower()
    vd = str(row.get("verdict_date") or "")[:10]
    stale = bool(vd) and vd < (today - timedelta(days=7)).isoformat()
    return {"bullish": "BULL", "bearish": "BEAR", "mixed": "NEUT"}.get(verdict, "—"), stale


def quant2_direction(row):
    """Weakness-repair LONG model: active rules + board rank = reversal conviction."""
    if not row:
        return "—"
    n = row.get("n_active_rules") or 0
    rank = row.get("rank") or 9999
    if n >= 6 and rank <= 100:
        return "BULL"
    if n >= 4:
        return "NEUT"
    return "—"


def stldc_direction(row):
    if not row:
        return "—"
    sig = (row.get("ldc_signal") or "").upper()
    return {"LONG": "BULL", "SHORT": "BEAR", "NEUT": "NEUT"}.get(sig, "—")


def fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Watchlist signal consensus board")
    ap.add_argument("--creds", default=None, help="Path to a file with supabase_url= / supabase_service_role= lines")
    ap.add_argument("--categories", default=",".join(WATCHLIST_CATEGORIES))
    ap.add_argument("--period", default="3mo")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--output-dir", default="reports")
    ap.add_argument("--skip-supabase", action="store_true", help="Do not upsert the board to Supabase")
    ap.add_argument("--limit", type=int, default=None, help="Cap universe size (debug)")
    args = ap.parse_args()

    today = datetime.now(_NY).date()
    sb = SB(*load_creds(args))

    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    wl = sb.select("watchlist", {"select": "symbol,category,exchange", "category": f"in.({','.join(cats)})"})
    universe, cats_by_sym = [], {}
    NON_US = {"OTC", "LSE", "STOCKHOLM", "XETR", "TORONTO", "TSX"}
    for r in wl:
        sym = (r.get("symbol") or "").strip().upper()
        exch = (r.get("exchange") or "").strip().upper()
        # NULL-exchange rows stay when the symbol is a plain US ticker;
        # only explicitly non-US exchanges are dropped.
        if not sym or (exch and exch not in US_EXCHANGES) or (not exch and ("." in sym or not sym.isalpha())):
            continue
        if sym in cats_by_sym:
            continue
        cats_by_sym[sym] = r["category"]
        universe.append(sym)
    if args.limit:
        universe = universe[: args.limit]
    print(f"Universe: {len(universe)} US-listed symbols from {cats}", file=sys.stderr)

    jlaw_y, jlaw_c, yd, cd = fetch_jlaw(sb)
    stldc, stldc_date = fetch_stldc(sb)
    quant2, q2_run = fetch_quant2(sb)
    flow, flow_date = fetch_flow(sb)
    spy_regime = Counter((r.get("spy_regime") or "?") for r in stldc.values()).most_common(1)
    print(
        f"Upstream: jlaw yahoo {yd} ({len(jlaw_y)}), jlaw chart {cd} ({len(jlaw_c)}), "
        f"stldc {stldc_date} ({len(stldc)}), quant2 {q2_run} ({len(quant2)}), "
        f"flow {flow_date} ({len(flow)}); SPY regime {spy_regime[0][0] if spy_regime else '?'}",
        file=sys.stderr,
    )

    global _T0
    _T0 = time.time()
    scan_by_sym = scan_symbols(universe, period=args.period, workers=args.workers)

    board = []
    for sym in universe:
        s = scan_by_sym.get(sym)
        jy, jc = jlaw_y.get(sym), jlaw_c.get(sym)
        fv, q2, st = flow.get(sym), quant2.get(sym), stldc.get(sym)
        fdir, fstale = flow_direction(fv, today)
        dirs = [
            scan_direction(s.get("score") if s else None),
            jlaw_yahoo_direction(jy),
            jlaw_chart_direction(jc),
            fdir,
            quant2_direction(q2),
            stldc_direction(st),
        ]
        votes = [d for d in dirs if d in ("BULL", "BEAR", "NEUT")]
        bull, bear, neut = votes.count("BULL"), votes.count("BEAR"), votes.count("NEUT")
        if bull and bull >= max(bear, neut):
            consensus = "BULL"
        elif bear and bear >= max(bull, neut):
            consensus = "BEAR"
        elif neut:
            consensus = "NEUT"
        else:
            consensus = "—"
        board.append(
            {
                "symbol": sym,
                "category": cats_by_sym.get(sym, ""),
                "scan_score": s.get("score") if s else None,
                "scan_rsi": s.get("rsi") if s else None,
                "scan_adx": s.get("adx") if s else None,
                "scan_price": s.get("price") if s else None,
                "next_earnings": s.get("next_earnings") if s else None,
                "scan_missing": s is None,
                "jlaw_yahoo_score": (jy or {}).get("jlaw_score"),
                "jlaw_yahoo_cls": (jy or {}).get("classification"),
                "jlaw_yahoo_stop": (jy or {}).get("stop_loss"),
                "jlaw_yahoo_target": (jy or {}).get("target_price"),
                "jlaw_chart_cls": (jc or {}).get("classification"),
                "jlaw_chart_vcp": (jc or {}).get("vcp_stage"),
                "flow_verdict": (fv or {}).get("verdict"),
                "flow_tilt": (fv or {}).get("premium_tilt"),
                "flow_stale": fstale,
                "quant2_rank": (q2 or {}).get("rank"),
                "quant2_rules": (q2 or {}).get("n_active_rules"),
                "quant2_grp": (q2 or {}).get("grp"),
                "stldc_dir": (st or {}).get("st_dir"),
                "stldc_signal": (st or {}).get("ldc_signal"),
                "stldc_score": (st or {}).get("score"),
                "stldc_structure": (st or {}).get("structure"),
                "directions": dirs,
                "votes": {"bull": bull, "bear": bear, "neut": neut},
                "consensus": consensus,
                "conflict": bull > 0 and bear > 0,
                "chart_veto": bool(jc and jc.get("classification") == "Skip") and dirs[1] == "BULL",
            }
        )

    board.sort(
        key=lambda r: (-(r["votes"]["bull"] - r["votes"]["bear"]), -(r["scan_score"] or 0), r["symbol"])
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_NY).strftime("%Y-%m-%d_%H%M")
    meta = {
        "generated_at": datetime.now(_NY).strftime("%Y-%m-%d %H:%M ET"),
        "data_delay": "15min (scan), upstream as of run dates",
        "universe_size": len(universe),
        "categories": cats,
        "scan_period": args.period,
        "run_dates": {
            "jlaw_yahoo": yd, "jlaw_chart": cd, "stldc": stldc_date,
            "quant2": q2_run, "flow_verdict": flow_date,
        },
        "spy_regime": spy_regime[0][0] if spy_regime else "?",
    }
    (out_dir / f"watchlist_signal_board_{ts}.json").write_text(
        json.dumps({"meta": meta, "board": board}, indent=2, default=str), encoding="utf-8"
    )
    print(f"JSON: {out_dir / f'watchlist_signal_board_{ts}.json'}", file=sys.stderr)

    md = [f"# Watchlist Signal Board — {today.isoformat()}", ""]
    md.append(f"*Generated {meta['generated_at']} · universe {len(universe)} symbols ({', '.join(cats)}) · scan period {args.period}*")
    md.append("")
    md.append(
        f"**Upstream runs:** jlaw yahoo `{yd}` · jlaw chart `{cd}` · stldc `{stldc_date}` · "
        f"quant2 `{q2_run}` · flow verdict `{flow_date}` · **SPY regime: {meta['spy_regime']}**"
    )
    md.append("")
    md.append("| # | Sym | Cat | Scan↑ | JLaw-Y | JLaw-C | Flow | Q2 | STLDC | Consensus | Flags |")
    md.append("|---|-----|-----|------:|--------|--------|------|----|-------|-----------|-------|")
    for i, r in enumerate(board, 1):
        flags = []
        if r["conflict"]:
            flags.append("CONFLICT")
        if r["chart_veto"]:
            flags.append("chart-veto")
        if r["flow_stale"]:
            flags.append("flow-stale")
        if r["scan_missing"]:
            flags.append("no-data")
        scan_cell = f"{r['scan_score']:.1f}" if r["scan_score"] is not None else "—"
        jy_cell = f"{r['jlaw_yahoo_score']} {r['jlaw_yahoo_cls'] or ''}".strip() if r["jlaw_yahoo_score"] is not None else "—"
        md.append(
            f"| {i} | **{r['symbol']}** | {r['category']} | {scan_cell} {scan_direction(r['scan_score'])} "
            f"| {jy_cell or '—'} | {r['jlaw_chart_cls'] or '—'} "
            f"| {r['flow_verdict'] or '—'} | {r['quant2_rank'] if r['quant2_rank'] is not None else '—'} ({r['quant2_rules'] or 0}r) "
            f"| {(r['stldc_signal'] or '—')} {(r['stldc_score'] if r['stldc_score'] is not None else '')} "
            f"| **{r['consensus']}** ({r['votes']['bull']}↑/{r['votes']['bear']}↓/{r['votes']['neut']}→) "
            f"| {','.join(flags) or ''} |"
        )
    md.append("")
    md.append(
        "**Column guide:** Scan = fresh bullish composite (/~9.5, trend+momentum). JLaw-Y = jlaw-v2-yahoo "
        "score + classification. JLaw-C = jlaw-v2-chart review. Flow = option-flow verdict (premium tilt in "
        "JSON). Q2 = quant2 leaderboard rank (active rules) — weakness-repair LONG model. STLDC = trend "
        "signal + score. Consensus = majority of directional votes; CONFLICT = bull & bear votes coexist; "
        "chart-veto = yahoo setup but chart Skip (chart always overrides)."
    )
    conflicts = [r for r in board if r["conflict"]]
    md.append("")
    md.append(f"## Conflicts ({len(conflicts)})")
    for r in conflicts:
        md.append(f"- **{r['symbol']}** ({r['category']}): {r['directions']}")
    (out_dir / f"watchlist_signal_board_{ts}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"MD:   {out_dir / f'watchlist_signal_board_{ts}.md'}", file=sys.stderr)

    if args.skip_supabase:
        print("Supabase upsert skipped (--skip-supabase)", file=sys.stderr)
    else:
        rows = build_rows(board, scan_by_sym, meta, today.isoformat())
        written = upsert_rows(sb, rows)
        print(f"Supabase upsert: {written} rows into {TABLE} (run_date={today.isoformat()})", file=sys.stderr)

    print(json.dumps({"meta": meta, "rows": len(board)}, indent=2))


if __name__ == "__main__":
    main()
