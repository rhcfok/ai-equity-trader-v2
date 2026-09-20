#!/usr/bin/env python3
"""Run the Yahoo Finance OHLCV-based JLaw breakout screen without n8n.

The model is deterministic and uses daily open, high, low, close, and equity
volume only. It does not use options data, chart images, LLMs, brokerage APIs,
or order-execution functionality.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MODEL_VERSION = "JLaw Yahoo OHLCV Model v2.0"
YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_OUTPUT_TABLE = "n8n-breakout-jlaw"
DEFAULT_WATCHLIST_TABLE = "watchlist"
SUPABASE_OUTPUT_COLUMNS = [
    "date", "symbol", "jlaw_score", "interpretation", "current_price",
    "pull_back_price", "entry_price", "stop_loss", "target_price",
    "rational", "rsi", "pct_from_52w_high", "above_50ma",
    "ma10_above_ma20", "vcp_stage", "pocket_pivot",
]
US_EXCHANGES = {
    "NASDAQ", "NASDAQGS", "NASDAQCM", "NASDAQGM", "NYSE", "NYSEARCA",
    "NYSE AMERICAN", "NYSE MKT", "AMEX", "ARCA", "BATS", "CBOE", "IEX",
    "US", "USA",
}


class RunnerError(RuntimeError):
    """Raise when configuration, data, or an external service is invalid."""


@dataclass
class RunReport:
    run_id: str
    run_date: str
    model_version: str
    source: str
    input_symbols: int = 0
    accepted_us_symbols: int = 0
    processed_symbols: int = 0
    skipped_symbols: list[dict[str, str]] = field(default_factory=list)
    failed_symbols: list[dict[str, str]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    supabase: dict[str, Any] = field(default_factory=dict)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def build_session() -> requests.Session:
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=0.7,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(("GET", "POST", "DELETE")), raise_on_status=False)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "Mozilla/5.0 JLawYahooRunner/2.0", "Accept": "application/json"})
    return session


def num(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def is_us_listing(item: dict[str, Any]) -> bool:
    venue = str(item.get("exchange") or item.get("primary_exchange") or item.get("mic") or item.get("country") or "").strip().upper()
    return venue in US_EXCHANGES or venue.startswith("NASDAQ") or venue.startswith("NYSE")


def deduplicate_watchlist(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, output = set(), []
    for item in items:
        symbol = normalize_symbol(item.get("symbol") or item.get("ticker"))
        if symbol and symbol not in seen:
            seen.add(symbol)
            copied = dict(item)
            copied["symbol"] = symbol
            output.append(copied)
    return output


def load_watchlist_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RunnerError(f"Watchlist file does not exist: {path}")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return rows
    raise RunnerError("Watchlist must be a CSV or JSON array of objects")


def supabase_headers(key: str, prefer: bool = False) -> dict[str, str]:
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = "return=minimal"
    return headers


def supabase_url(base_url: str, table: str) -> str:
    base = base_url.rstrip("/")
    return f"{base if base.endswith('/rest/v1') else base + '/rest/v1'}/{table}"


def load_watchlist_supabase(session: requests.Session, base_url: str, key: str, table: str) -> list[dict[str, Any]]:
    response = session.get(supabase_url(base_url, table), headers=supabase_headers(key), params={"select": "*"}, timeout=30)
    if not response.ok:
        raise RunnerError(f"Supabase watchlist read failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    if not isinstance(payload, list):
        raise RunnerError("Supabase watchlist response was not a JSON array")
    return [row for row in payload if isinstance(row, dict)]


def fetch_existing_symbols(session: requests.Session, base_url: str, key: str, table: str, run_date: str) -> set[str]:
    response = session.get(supabase_url(base_url, table), headers=supabase_headers(key), params={"select": "symbol", "date": f"eq.{run_date}"}, timeout=30)
    if not response.ok:
        raise RunnerError(f"Supabase duplicate check failed ({response.status_code}): {response.text[:500]}")
    rows = response.json()
    if not isinstance(rows, list):
        raise RunnerError("Supabase duplicate check response was not a JSON array")
    return {normalize_symbol(row.get("symbol")) for row in rows if isinstance(row, dict)}


def write_results_supabase(session: requests.Session, base_url: str, key: str, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    response = session.post(supabase_url(base_url, table), headers=supabase_headers(key, prefer=True), json=rows, timeout=30)
    if not response.ok:
        raise RunnerError(f"Supabase write failed ({response.status_code}): {response.text[:500]}")


def fetch_yahoo_history(session: requests.Session, symbol: str, history_range: str, base_url: str) -> list[dict[str, Any]] | None:
    """Fetch daily Yahoo Finance OHLCV records; return None for an unavailable symbol."""
    response = session.get(f"{base_url.rstrip('/')}/{quote(symbol, safe='^')}", params={
        "interval": "1d", "range": history_range, "includeAdjustedClose": "true", "events": "div,splits",
    }, timeout=30)
    if response.status_code in (404, 422):
        return None
    if not response.ok:
        raise RunnerError(f"Yahoo Finance request failed ({response.status_code}): {response.text[:500]}")
    chart = response.json().get("chart", {})
    if chart.get("error"):
        return None
    records = chart.get("result") or []
    if not records:
        return None
    record = records[0]
    timestamps = record.get("timestamp") or []
    quote_data = ((record.get("indicators") or {}).get("quote") or [{}])[0]
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        try:
            row = {
                "date": datetime.fromtimestamp(timestamp, timezone.utc).date(),
                "open": num((quote_data.get("open") or [])[index]),
                "high": num((quote_data.get("high") or [])[index]),
                "low": num((quote_data.get("low") or [])[index]),
                "close": num((quote_data.get("close") or [])[index]),
                "volume": num((quote_data.get("volume") or [])[index]),
            }
        except IndexError:
            continue
        if all(row[name] is not None for name in ("open", "high", "low", "close", "volume")) and row["volume"] >= 0:
            rows.append(row)
    return rows or None


def sma(values: list[float], period: int) -> float | None:
    return mean(values[-period:]) if len(values) >= period else None


def ema_series(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return output
    current = mean(values[:period])
    output[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = current + alpha * (values[index] - current)
        output[index] = current
    return output


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return output
    gains = [0.0] + [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [0.0] + [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain, avg_loss = mean(gains[1:period + 1]), mean(losses[1:period + 1])
    def calc(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)
    output[period] = calc(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        output[index] = calc(avg_gain, avg_loss)
    return output


def adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    length = len(closes)
    output: list[float | None] = [None] * length
    if length < 2 * period + 1:
        return output
    tr, plus_dm, minus_dm = [0.0] * length, [0.0] * length, [0.0] * length
    for index in range(1, length):
        up_move, down_move = highs[index] - highs[index - 1], lows[index - 1] - lows[index]
        tr[index] = max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1]))
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
    smooth_tr, smooth_plus, smooth_minus = sum(tr[1:period + 1]), sum(plus_dm[1:period + 1]), sum(minus_dm[1:period + 1])
    dx: list[float | None] = [None] * length
    for index in range(period, length):
        if index > period:
            smooth_tr = smooth_tr - smooth_tr / period + tr[index]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[index]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[index]
        plus_di = 100 * smooth_plus / smooth_tr if smooth_tr else 0.0
        minus_di = 100 * smooth_minus / smooth_tr if smooth_tr else 0.0
        denominator = plus_di + minus_di
        dx[index] = 100 * abs(plus_di - minus_di) / denominator if denominator else 0.0
    start = 2 * period - 1
    current = mean([value for value in dx[period:start + 1] if value is not None])
    output[start] = current
    for index in range(start + 1, length):
        current = ((current * (period - 1)) + (dx[index] or 0.0)) / period
        output[index] = current
    return output


def latest_valid(values: list[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None), None)


def pct_change(new: float, old: float) -> float | None:
    return ((new / old) - 1) * 100 if old else None


def quarter_vwap(rows: list[dict[str, Any]]) -> float | None:
    last_date = rows[-1]["date"]
    quarter_month = ((last_date.month - 1) // 3) * 3 + 1
    quarter_start = last_date.replace(month=quarter_month, day=1)
    selected = [row for row in rows if row["date"] >= quarter_start and row["volume"] > 0]
    denominator = sum(row["volume"] for row in selected)
    if not denominator:
        return None
    return sum(((row["high"] + row["low"] + row["close"]) / 3) * row["volume"] for row in selected) / denominator


def score_jlaw(rows: list[dict[str, Any]], symbol: str, run_date: str) -> dict[str, Any]:
    """Apply the original chart-only 0–16 JLaw rubric from Yahoo daily OHLCV."""
    if len(rows) < 252:
        raise RunnerError(f"Yahoo Finance returned only {len(rows)} valid daily bars; 252 are required")
    closes = [row["close"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]
    volumes = [row["volume"] for row in rows]
    current, previous = closes[-1], closes[-2]
    sma10, sma20, sma50, sma100, sma200 = (sma(closes, period) for period in (10, 20, 50, 100, 200))
    rsi = latest_valid(rsi_series(closes))
    adx_values, adx = adx_series(highs, lows, closes), None
    adx = latest_valid(adx_values)
    fast, slow = ema_series(closes, 12), ema_series(closes, 26)
    macd = [fast[i] - slow[i] if fast[i] is not None and slow[i] is not None else None for i in range(len(closes))]
    macd_values = [value if value is not None else 0.0 for value in macd]
    signal = ema_series(macd_values[25:], 9)
    histogram: list[float | None] = [None] * 25 + [macd_values[i + 25] - signal[i] if signal[i] is not None else None for i in range(len(signal))]
    vwap = quarter_vwap(rows)
    base_rows = rows[-30:]
    base_high, base_low = max(row["high"] for row in base_rows), min(row["low"] for row in base_rows)
    base_range = (base_high / base_low - 1) if base_low else float("inf")
    base_net_move = abs(closes[-1] / closes[-30] - 1) if closes[-30] else float("inf")
    base_building = base_range <= 0.20 and base_net_move <= 0.12
    ma_values = [value for value in (sma10, sma20, sma50) if value is not None]
    ma_pinch = len(ma_values) == 3 and (max(ma_values) / min(ma_values) - 1) <= 0.06
    adx_recent = [value for value in adx_values[-5:] if value is not None]
    adx_previous = [value for value in adx_values[-10:-5] if value is not None]
    adx_low_or_falling = bool(adx is not None and (adx < 20 or (adx_recent and adx_previous and mean(adx_recent) < mean(adx_previous))))
    dry_up = mean(volumes[-10:]) <= mean(volumes[-30:-10]) * 0.75
    last_hist, hist_5 = latest_valid(histogram), latest_valid(histogram[:-5])
    macd_contracting = bool(last_hist is not None and hist_5 is not None and abs(last_hist) <= abs(hist_5) and abs(last_hist) <= current * 0.005)
    rsi_stable = bool(rsi is not None and 45 <= rsi <= 55)
    pivot = max(highs[-21:-1])
    breakout = current > pivot and vwap is not None and current > vwap
    breakout_volume = volumes[-1] >= 1.5 * mean(volumes[-21:-1])
    extended = current > pivot * 1.05
    above_50_200 = bool(sma50 is not None and sma200 is not None and current > sma50 and current > sma200)
    section_a = (2 if above_50_200 else 0) + (2 if base_building else 0)
    section_b = (2 if ma_pinch else 0) + (2 if adx_low_or_falling else 0)
    section_c = (2 if dry_up else 0) + (2 if (macd_contracting or rsi_stable) else 0)
    section_d = (2 if breakout else 0) + (2 if breakout_volume else 0)
    if extended:
        section_d = max(0, section_d - 3)
    score = max(0, min(16, section_a + section_b + section_c + section_d))
    interpretation = ("Skip — setup is immature, broken, or extended." if score <= 9 else
                      "Watch / Small size — energy is building near a potential pivot." if score <= 12 else
                      "Valid trade — quantitative breakout conditions are aligned." if score <= 14 else
                      "Aggressive A+ setup — compression and activation are aligned.")
    ranges = []
    for window in (rows[-60:-45], rows[-45:-30], rows[-30:-15], rows[-15:]):
        low, high = min(row["low"] for row in window), max(row["high"] for row in window)
        ranges.append(high / low - 1 if low else float("inf"))
    contractions = all(ranges[index] <= ranges[index - 1] for index in range(1, len(ranges)))
    if breakout and not extended:
        vcp_stage = "breakout"
    elif base_building and contractions and dry_up:
        vcp_stage = "mature"
    elif base_building:
        vcp_stage = "developing"
    elif sma50 is not None and current > sma50:
        vcp_stage = "early"
    else:
        vcp_stage = "none"
    prior_down_volumes = [volumes[index] for index in range(len(rows) - 11, len(rows) - 1) if closes[index] < closes[index - 1]]
    pocket_pivot = bool(current > previous and prior_down_volumes and volumes[-1] > max(prior_down_volumes))
    pullback = max(value for value in (sma20, vwap, base_low) if value is not None)
    entry = pivot * 1.001
    stop = min(base_low * 0.99, (sma50 or base_low) * 0.99)
    if stop >= entry:
        stop = entry * 0.93
    target = entry + 2 * (entry - stop)
    pct_from_high = pct_change(current, max(highs[-252:]))
    daily_change = pct_change(current, previous)
    rational = " ".join([
        f"A {section_a}/4: price {'above' if above_50_200 else 'not above'} 50/200 SMA; {'4–6 week base detected' if base_building else 'no qualifying base'}.",
        f"B {section_b}/4: 10/20/50 SMA {'pinched' if ma_pinch else 'not pinched'}; ADX {'low/falling' if adx_low_or_falling else 'not low/falling'}.",
        f"C {section_c}/4: equity volume {'drying up' if dry_up else 'not drying up'}; MACD/RSI {'contracting or stable' if (macd_contracting or rsi_stable) else 'not qualifying'}.",
        f"D {section_d}/4: price/VWAP breakout {'confirmed' if breakout else 'not confirmed'}; breakout volume {'>=1.5x' if breakout_volume else '<1.5x'}; {'late-extension penalty applied.' if extended else 'no late-extension penalty.'}",
    ])
    return {
        "date": run_date, "symbol": normalize_symbol(symbol), "jlaw_score": int(score),
        "jlaw_score_interpretation": interpretation, "interpretation": interpretation,
        "current_price": rounded(current), "pull_back_price": rounded(pullback), "entry_price": rounded(entry),
        "stop_loss": rounded(stop), "target_price": rounded(target), "rational": rational,
        "rsi": rounded(rsi), "pct_from_52w_high": rounded(pct_from_high), "above_50ma": bool(sma50 is not None and current > sma50),
        "ma10_above_ma20": bool(sma10 is not None and sma20 is not None and sma10 > sma20),
        "vcp_stage": vcp_stage, "pocket_pivot": pocket_pivot, "data_source": "Yahoo Finance",
        "data_as_of": rows[-1]["date"].isoformat(), "data_status": "ok", "model_version": MODEL_VERSION,
        "metrics": {
            "sma10": rounded(sma10), "sma20": rounded(sma20), "sma50": rounded(sma50), "sma100": rounded(sma100), "sma200": rounded(sma200),
            "adx14": rounded(adx), "macd_histogram": rounded(last_hist, 4), "anchored_quarter_vwap": rounded(vwap),
            "daily_change_pct": rounded(daily_change), "base_range_pct": rounded(base_range * 100), "equity_volume": int(volumes[-1]),
            "average_volume_20d": rounded(mean(volumes[-21:-1]), 0), "pivot_price": rounded(pivot), "bars": len(rows),
        },
    }


def no_data_result(symbol: str, run_date: str) -> dict[str, Any]:
    return {
        "date": run_date, "symbol": normalize_symbol(symbol), "jlaw_score": 0,
        "jlaw_score_interpretation": "Skip — Yahoo Finance daily OHLCV unavailable.",
        "interpretation": "Skip — Yahoo Finance daily OHLCV unavailable.",
        "current_price": None, "pull_back_price": None, "entry_price": None, "stop_loss": None, "target_price": None,
        "rational": "Yahoo Finance returned no usable daily OHLCV series. No setup was scored and no price levels were produced.",
        "rsi": None, "pct_from_52w_high": None, "above_50ma": None, "ma10_above_ma20": None,
        "vcp_stage": "none", "pocket_pivot": False, "data_source": "Yahoo Finance", "data_as_of": None,
        "data_status": "unavailable", "model_version": MODEL_VERSION, "metrics": {},
    }


def supabase_row(result: dict[str, Any]) -> dict[str, Any]:
    return {column: result.get(column) for column in SUPABASE_OUTPUT_COLUMNS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Yahoo Finance OHLCV-based JLaw breakout screen without n8n.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--watchlist", type=Path, help="CSV/JSON watchlist with symbol/ticker and US exchange fields.")
    source.add_argument("--symbols", help="Comma-separated US tickers for an ad hoc run.")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"), help="Optional .env file; process environment wins.")
    parser.add_argument("--output", type=Path, default=Path("jlaw_yahoo_run.json"), help="Full local JSON audit output.")
    parser.add_argument("--run-date", default=date.today().isoformat(), help="Output date in YYYY-MM-DD format.")
    parser.add_argument("--history-range", default="2y", choices=("1y", "2y", "5y"), help="Yahoo daily history range; 2y is recommended.")
    parser.add_argument("--yahoo-base-url", default=optional_env("YAHOO_FINANCE_BASE_URL", YAHOO_CHART_BASE_URL), help="Yahoo Finance chart API base URL.")
    parser.add_argument("--write-supabase", action="store_true", help="Explicitly append non-duplicate results to Supabase.")
    parser.add_argument("--watchlist-table", default=optional_env("SUPABASE_WATCHLIST_TABLE", DEFAULT_WATCHLIST_TABLE))
    parser.add_argument("--output-table", default=optional_env("SUPABASE_OUTPUT_TABLE", DEFAULT_OUTPUT_TABLE))
    parser.add_argument("--no-skip-existing", action="store_true", help="Allow duplicate same-date writes only when explicitly intended.")
    parser.add_argument("--request-interval", type=float, default=0.25, help="Seconds between Yahoo requests.")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.dotenv)
    configure_logging(args.log_level)
    if args.request_interval < 0:
        raise RunnerError("--request-interval cannot be negative")
    try:
        datetime.strptime(args.run_date, "%Y-%m-%d")
    except ValueError as exc:
        raise RunnerError("--run-date must follow YYYY-MM-DD") from exc
    session, run_id = build_session(), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = RunReport(run_id=run_id, run_date=args.run_date, model_version=MODEL_VERSION, source="Yahoo Finance /v8/finance/chart")
    supabase_base, supabase_key = optional_env("SUPABASE_URL"), optional_env("SUPABASE_KEY")
    if args.symbols:
        raw_watchlist = [{"symbol": value, "exchange": "US"} for value in args.symbols.split(",")]
    elif args.watchlist:
        raw_watchlist = load_watchlist_file(args.watchlist)
    else:
        if not (supabase_base and supabase_key):
            raise RunnerError("Provide --watchlist or --symbols, or configure SUPABASE_URL and SUPABASE_KEY")
        raw_watchlist = load_watchlist_supabase(session, supabase_base, supabase_key, args.watchlist_table)
    watchlist = deduplicate_watchlist(raw_watchlist)
    report.input_symbols = len(watchlist)
    accepted = []
    for item in watchlist:
        if is_us_listing(item):
            accepted.append(item)
        else:
            report.skipped_symbols.append({"symbol": normalize_symbol(item.get("symbol")), "reason": "non-US or unverified exchange"})
    report.accepted_us_symbols = len(accepted)
    for index, item in enumerate(accepted):
        symbol = normalize_symbol(item["symbol"])
        try:
            history = fetch_yahoo_history(session, symbol, args.history_range, args.yahoo_base_url)
            result = score_jlaw(history, symbol, args.run_date) if history else no_data_result(symbol, args.run_date)
            report.results.append(result)
            report.processed_symbols += 1
            logging.info("%s: score=%s status=%s", symbol, result["jlaw_score"], result["data_status"])
        except (requests.RequestException, ValueError, RunnerError) as exc:
            logging.error("%s: %s", symbol, exc)
            report.failed_symbols.append({"symbol": symbol, "reason": str(exc)})
        if index < len(accepted) - 1 and args.request_interval:
            time.sleep(args.request_interval)
    report.supabase = {"requested": args.write_supabase, "written": 0, "skipped_existing": 0}
    if args.write_supabase:
        if not (supabase_base and supabase_key):
            raise RunnerError("SUPABASE_URL and SUPABASE_KEY are required with --write-supabase")
        rows = [supabase_row(result) for result in report.results]
        if not args.no_skip_existing:
            existing = fetch_existing_symbols(session, supabase_base, supabase_key, args.output_table, args.run_date)
            kept = [row for row in rows if normalize_symbol(row.get("symbol")) not in existing]
            report.supabase["skipped_existing"] = len(rows) - len(kept)
            rows = kept
        write_results_supabase(session, supabase_base, supabase_key, args.output_table, rows)
        report.supabase["written"] = len(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asdict(report), indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": report.run_id, "run_date": report.run_date, "input_symbols": report.input_symbols,
                      "accepted_us_symbols": report.accepted_us_symbols, "processed_symbols": report.processed_symbols,
                      "failed_symbols": len(report.failed_symbols), "results_written": report.supabase["written"], "output": str(args.output)}))
    return 2 if report.failed_symbols else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s", stream=sys.stderr)
        logging.error("%s", exc)
        raise SystemExit(1)
