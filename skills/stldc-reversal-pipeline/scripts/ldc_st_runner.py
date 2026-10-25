#!/usr/bin/env python3
"""Runner for the Lorentzian Classification + Supertrend strategy.

For each watchlist ticker the script:
  1. Downloads history via yfinance (default: 5y daily; INTERVAL configurable).
  2. Runs the LDC engine (ldc_engine.py - faithful port of jdehorty's
     "Machine Learning: Lorentzian Classification" v2.0) and the classic
     Supertrend (ATR 10 / x3, hl2 source).
  3. Simulates one position per ticker through history:
       entry long  = LDC startLongTrade  AND Supertrend bullish
       entry short = LDC startShortTrade AND Supertrend bearish
       exit long   = LDC strict 4-bar exit OR opposite signal OR ST bearish
       exit short  = mirrored (opposite-signal flips close and reopen)
  4. POSTs a Discord embed for every entry/exit that lands on the most
     recently CLOSED bar, and prints a per-ticker state summary.

Usage
-----
    python ldc_st_runner.py                     # one scan, console + Discord
    DISCORD_WEBHOOK_URL=... python ldc_st_runner.py

Signal frequency note
---------------------
With Pine defaults the LDC signal only changes at prediction sign flips and
the kernel filter ("Trade with Kernel") further requires the kernel slope to
agree on daily bars, so entries are genuinely sparse (roughly one per ticker
per year on 5y daily). Levers, in increasing aggressiveness:
  * USE_KERNEL_FILTER = False   (the Pine "Trade with Kernel" toggle)
  * USE_VOLATILITY_FILTER / USE_REGIME_FILTER = False (the Pine filter toggles)
  * a lower INTERVAL (the indicator's sweet spot is intraday, e.g. "1h"
    with PERIOD="720d").
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timezone

import pandas as pd
import requests
import yfinance as yf

from ldc_engine import LDCSettings, SupertrendSettings, compute_signals

try:
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    import pytz
    MARKET_TZ = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TICKERS: list[str] = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
    "GOOGL", "META", "AMD", "NFLX", "JPM",
]

INTERVAL: str = "1d"     # "1d", "1h", "30m", "15m", ...
PERIOD: str = "5y"       # daily; respect Yahoo caps intraday (e.g. "720d" for 1h)

USE_SUPERTREND_FILTER: bool = True    # gate LDC entries by Supertrend direction
EXIT_ON_ST_FLIP: bool = True          # LDC exits also fire when ST turns against the trade

# Pine "Filters" toggles (defaults replicate the indicator)
USE_KERNEL_FILTER: bool = True
USE_VOLATILITY_FILTER: bool = True
USE_REGIME_FILTER: bool = True

DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
SEND_EXITS: bool = True

log = logging.getLogger("ldc_runner")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def fetch(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=True, threads=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df.rename(columns=str.lower)
    if df.empty:
        raise RuntimeError("no usable OHLCV rows")

    # Only fully closed candles are evaluated.
    if interval.endswith("d"):
        now_et = datetime.now(MARKET_TZ)
        session_live = now_et.weekday() < 5 and dtime(9, 30) <= now_et.time() < dtime(16, 0)
        if session_live and pd.Timestamp(df.index[-1]).tz_localize(None).date() == now_et.date():
            df = df.iloc[:-1]
    else:
        bar = pd.Timedelta(interval).to_pytimedelta()
        now = pd.Timestamp.now(tz=df.index.tz)
        if df.index[-1] + pd.Timedelta(seconds=bar.total_seconds()) > now:
            df = df.iloc[:-1]
    return df


# ---------------------------------------------------------------------------
# Position simulation (one position per ticker, stop-and-reverse)
# ---------------------------------------------------------------------------
def track_positions(out: pd.DataFrame) -> tuple[list[tuple[pd.Timestamp, str]], int]:
    """Replay entry/exit states chronologically; return (events, final position).
    Only transitions while a matching position is open become events."""
    events: list[tuple[pd.Timestamp, str]] = []
    pos = 0
    for ts, row in out.iterrows():
        if pos == 1 and row["exit_long"]:
            events.append((ts, "close_long"))
            pos = 0
        elif pos == -1 and row["exit_short"]:
            events.append((ts, "close_short"))
            pos = 0
        if pos == 0 and row["entry_long"]:
            events.append((ts, "entry_long"))
            pos = 1
        elif pos == 0 and row["entry_short"]:
            events.append((ts, "entry_short"))
            pos = -1
        elif pos == 1 and row["entry_short"]:
            events.append((ts, "close_long"))
            events.append((ts, "entry_short"))
            pos = -1
        elif pos == -1 and row["entry_long"]:
            events.append((ts, "close_short"))
            events.append((ts, "entry_long"))
            pos = 1
    return events, pos


# ---------------------------------------------------------------------------
# Discord delivery
# ---------------------------------------------------------------------------
def send_discord(ticker: str, event: str, row: pd.Series) -> None:
    is_entry = event in ("entry_long", "entry_short")
    title = ("🟢 LDC+ST LONG — " if event == "entry_long" else
             "🔴 LDC+ST SHORT — " if event == "entry_short" else
             "✅ Close LONG — " if event == "close_long" else
             "✅ Close SHORT — ") + ticker
    color = 0x2ECC71 if event == "entry_long" else 0xE74C3C if event == "entry_short" else 0x95A5A6
    state = ("LONG" if row["signal"] == 1 else "SHORT" if row["signal"] == -1 else "neutral")
    fields = [
        {"name": "LDC model", "inline": True, "value":
            f"prediction **{row['prediction']:+.0f}** | signal {state}\n"
            f"kernel {'bullish' if row['kernel_bullish'] else 'bearish'} | bars held {int(row['bars_held'])}"},
        {"name": "Filters", "inline": True, "value":
            f"volatility {'✓' if row['filter_volatility'] else '✗'} | "
            f"regime {'✓' if row['filter_regime'] else '✗'} | "
            f"adx {'✓' if row['filter_adx'] else '✗'}"},
        {"name": "Supertrend / price", "inline": False, "value":
            f"ST direction {'BULLISH' if row['st_dir'] == 1 else 'BEARISH'} | "
            f"close {row['close']:,.2f}"},
    ]
    payload = {
        "username": "LDC+ST Screener",
        "embeds": [{
            "title": title, "color": color, "fields": fields,
            "footer": {"text": "Lorentzian Classification + Supertrend runner"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()
    log.info("%s: Discord embed delivered (HTTP %s)", ticker, resp.status_code)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not set - events will only be logged to the console.")

    settings = LDCSettings(use_kernel_filter=USE_KERNEL_FILTER,
                           use_volatility_filter=USE_VOLATILITY_FILTER,
                           use_regime_filter=USE_REGIME_FILTER)
    events_total = 0
    for ticker in TICKERS:
        try:
            df = fetch(ticker, PERIOD, INTERVAL)
        except Exception as exc:
            log.error("%s: data fetch failed (%s)", ticker, exc)
            continue

        out = compute_signals(df, settings, SupertrendSettings(),
                              use_supertrend_filter=USE_SUPERTREND_FILTER,
                              exit_on_st_flip=EXIT_ON_ST_FLIP)
        out["close"] = df["close"]
        events, pos = track_positions(out)
        last = out.iloc[-1]
        last_ts = out.index[-1]
        last_events = [e for t, e in events if t == last_ts]
        pos_state = {0: "flat", 1: "LONG", -1: "SHORT"}[pos]

        entries = [(t, e) for t, e in events if e.startswith("entry")]
        recent_txt = "; ".join(f"{t:%Y-%m-%d} {e.replace('entry_', '').upper()}"
                               for t, e in entries[-3:][::-1]) or "none"
        log.info("%s: LDC signal=%+d prediction=%+d ST=%+d | position=%s | "
                 "entries L/S=%d/%d | recent: %s",
                 ticker, int(last["signal"]), int(last["prediction"]), int(last["st_dir"]),
                 pos_state,
                 sum(1 for _, e in entries if e == "entry_long"),
                 sum(1 for _, e in entries if e == "entry_short"), recent_txt)

        if last_events:
            log.info("%s: *** %s on %s ***", ticker, ", ".join(last_events).upper(), last_ts.date())
            print(f">>> {ticker} @ {last_ts}: {', '.join(last_events)}")
            if DISCORD_WEBHOOK_URL:
                for event in last_events:
                    try:
                        send_discord(ticker, event, last)
                        time.sleep(1.0)
                    except requests.RequestException as exc:
                        log.error("%s: Discord delivery failed (%s)", ticker, exc)
            events_total += len(last_events)

    log.info("Scan finished: %d event(s) on the last closed bar across %d tickers.",
             events_total, len(TICKERS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
