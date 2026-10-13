#!/usr/bin/env python3
"""Backtest predeclared monthly-box rules on the Supabase US watchlist.

The runner is intentionally read-only with respect to Supabase. It downloads
Yahoo v8 daily OHLCV, caches source data, evaluates three fixed long-only rules,
and produces development/holdout audit artifacts. A failed validation is a valid
research result and exits successfully; operational/data failures exit non-zero.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import os
import platform
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_CATEGORIES = ("core", "satellite", "watch1")
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "CBOE", "IEX", "US", "USA"}
WATCHLIST_PAGE_SIZE = 1000
STRATEGIES = (
    "prior_month_high_breakout",
    "five_day_initial_balance_breakout",
    "monthly_open_reclaim",
)
STRATEGY_LABELS = {
    "prior_month_high_breakout": "Prior-month high breakout",
    "five_day_initial_balance_breakout": "Five-day initial-balance breakout",
    "monthly_open_reclaim": "Monthly-open reclaim",
}
INDUCTION_HISTORY_YEARS = 10
INDUCTION_HOLDOUT_YEARS = 2
INDUCTION_COST_BPS = 20.0
INDUCTION_BOOTSTRAP_DRAWS = 2000
MAX_TICKER_CONCENTRATION = 0.25
MAX_CATEGORY_CONCENTRATION = 0.60


class BacktestError(RuntimeError):
    """Raised for an operational error that makes a run non-auditable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("$")


def canonical_agent_id(value: Any, field_name: str) -> str:
    """Accept a stable, nonblank agent/job identifier without whitespace aliases."""
    if not isinstance(value, str):
        raise BacktestError(f"{field_name} must be a string")
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise BacktestError(f"{field_name} must be a nonblank canonical no-whitespace identifier")
    normalized = value
    if not normalized:
        raise BacktestError(f"{field_name} must not be blank")
    if len(normalized) > 128:
        raise BacktestError(f"{field_name} must be a canonical no-whitespace identifier")
    return normalized


def is_us_listing(row: dict[str, Any]) -> bool:
    exchange = str(row.get("exchange") or row.get("primary_exchange") or row.get("mic") or "").strip().upper()
    return exchange in US_EXCHANGES or exchange.startswith("NASDAQ") or exchange.startswith("NYSE")


def prepare_watchlist(
    rows: list[dict[str, Any]], categories: tuple[str, ...], max_symbols: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Filter to US rows, order category priority, and deduplicate symbols."""
    if not categories:
        raise BacktestError("At least one watchlist category is required")
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    requested = {category.lower() for category in categories}

    for raw in rows:
        category = str(raw.get("category") or "").strip().lower()
        symbol = normalize_symbol(raw.get("symbol") or raw.get("ticker"))
        exchange = str(raw.get("exchange") or raw.get("primary_exchange") or raw.get("mic") or "").strip().upper()
        if category not in requested:
            rejected.append({"symbol": symbol, "category": category, "exchange": exchange, "reason": "category_not_selected"})
        elif not symbol:
            rejected.append({"symbol": symbol, "category": category, "exchange": exchange, "reason": "blank_symbol"})
        elif not is_us_listing(raw):
            rejected.append({"symbol": symbol, "category": category, "exchange": exchange, "reason": "not_explicitly_us_listed"})

    for category in categories:
        for raw in rows:
            row_category = str(raw.get("category") or "").strip().lower()
            symbol = normalize_symbol(raw.get("symbol") or raw.get("ticker"))
            if row_category != category or not symbol or not is_us_listing(raw):
                continue
            if symbol in seen:
                rejected.append({
                    "symbol": symbol,
                    "category": row_category,
                    "exchange": str(raw.get("exchange") or "").strip().upper(),
                    "reason": "duplicate_lower_priority_or_later_row",
                })
                continue
            seen.add(symbol)
            accepted.append({
                "ticker": symbol,
                "category": category,
                "exchange": str(raw.get("exchange") or "").strip().upper(),
                "watchlist_id": str(raw.get("id") or ""),
            })
    return accepted, rejected


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def resolve_supabase_credentials(env_file: Path | None) -> tuple[str, str]:
    fallback = load_env_file(env_file) if env_file else {}
    url = os.environ.get("SUPABASE_URL") or fallback.get("SUPABASE_URL") or fallback.get("supabase_url") or ""
    key = (
        os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or fallback.get("SUPABASE_KEY")
        or fallback.get("SUPABASE_SERVICE_ROLE_KEY")
        or fallback.get("supabase_service_role")
        or ""
    )
    if not url or not key:
        raise BacktestError("SUPABASE_URL and SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY are required")
    return url.rstrip("/"), key


def load_watchlist_supabase(base_url: str, key: str, table: str, categories: tuple[str, ...]) -> list[dict[str, Any]]:
    if not table or not table.replace("_", "").isalnum() or table[0].isdigit():
        raise BacktestError("Watchlist table must be a simple SQL identifier")
    params = urllib.parse.urlencode({
        "select": "id,symbol,exchange,category",
        "category": "in.(" + ",".join(categories) + ")",
        "order": "id.asc",
    })
    endpoint = f"{base_url}/rest/v1/{table}?{params}"
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        request = urllib.request.Request(endpoint, headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Prefer": "count=exact",
            "Range-Unit": "items",
            "Range": f"{offset}-{offset + WATCHLIST_PAGE_SIZE - 1}",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
                content_range = response.headers.get("Content-Range", "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise BacktestError(f"Supabase watchlist request failed: HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BacktestError(f"Supabase watchlist request failed: {exc}") from exc
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise BacktestError("Supabase watchlist response was not a JSON array of objects")
        rows.extend(payload)
        total_text = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        if total_text.isdigit() and len(rows) >= int(total_text):
            return rows
        if len(payload) < WATCHLIST_PAGE_SIZE:
            return rows
        offset += len(payload)


def load_watchlist_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise BacktestError(f"Watchlist file does not exist: {path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise BacktestError("Watchlist JSON must be an array of objects")
        return payload
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    raise BacktestError("Watchlist file must be CSV or JSON")


def daily_cache_path(raw_dir: Path, ticker: str) -> Path:
    return raw_dir / f"{ticker}.csv"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_raw_history(data: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """Reject stale, malformed, or materially gapped cache rows.

    In the portable runner, one absent weekday between adjacent bars is allowed
    and emitted for audit because US market holidays are not all federal
    holidays. Two or more consecutive missing weekdays are not silently
    tolerated: they require a complete source refresh or explicit review.
    """
    expected = {"date", "open", "high", "low", "close", "volume", "adjclose"}
    missing = expected - set(data.columns)
    if missing:
        raise BacktestError(f"raw history is missing columns: {','.join(sorted(missing))}")
    frame = data[list(expected)].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for column in ("open", "high", "low", "close", "volume", "adjclose"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.isna().any().any():
        raise BacktestError("raw history contains missing or non-numeric OHLCV/adjusted-close values")
    frame = frame.sort_values("date").reset_index(drop=True)
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise BacktestError("raw history dates must be unique and ascending")
    if (frame[["open", "high", "low", "close", "adjclose"]] <= 0).any().any() or (frame["volume"] < 0).any():
        raise BacktestError("raw history contains non-positive prices or negative volume")
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any() or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        raise BacktestError("raw history violates OHLC invariants")
    if frame.iloc[0]["date"] > start + pd.Timedelta(days=10):
        raise BacktestError(f"raw history starts too late ({frame.iloc[0]['date'].date()})")
    if frame.iloc[-1]["date"] < end - pd.Timedelta(days=7):
        raise BacktestError(f"raw history is stale or ends too early ({frame.iloc[-1]['date'].date()})")
    permitted_gaps: list[dict[str, str]] = []
    dates = frame["date"].tolist()
    for previous, following in zip(dates, dates[1:]):
        absent_weekdays = pd.bdate_range(previous + pd.Timedelta(days=1), following - pd.Timedelta(days=1))
        if len(absent_weekdays) > 1:
            raise BacktestError(
                f"raw history has {len(absent_weekdays)} consecutive missing weekdays from "
                f"{absent_weekdays[0].date()} to {absent_weekdays[-1].date()}"
            )
        if len(absent_weekdays) == 1:
            permitted_gaps.append({
                "after_date": previous.date().isoformat(), "before_date": following.date().isoformat(),
                "missing_weekday": absent_weekdays[0].date().isoformat(),
            })
    return frame, permitted_gaps


def continuous_price_basis(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply Yahoo's adjclose/close factor consistently to all tradable OHLC fields.

    The resulting series is a continuous total-return-like price basis: it avoids
    split and ex-dividend discontinuities in both technical levels and returns.
    The raw cache is preserved and every material factor change is emitted for
    a later broker-quality corporate-action audit.
    """
    frame = data.copy()
    factor = frame["adjclose"] / frame["close"]
    if factor.isna().any() or (factor <= 0).any() or not np.isfinite(factor).all():
        raise BacktestError("invalid Yahoo adjustment factor")
    for column in ("open", "high", "low", "close"):
        frame[f"raw_{column}"] = frame[column]
        frame[column] = frame[column] * factor
    frame["adjustment_factor"] = factor
    factor_change = factor.pct_change().fillna(0.0)
    events = frame.loc[factor_change.abs() > 0.0001, ["date", "adjustment_factor", "raw_close", "close"]].copy()
    events.insert(0, "factor_change", factor_change.loc[events.index])
    return frame, events


def fetch_yahoo_daily(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    period1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    period2 = int(pd.Timestamp(end + pd.Timedelta(days=1), tz="UTC").timestamp())
    query = urllib.parse.urlencode({
        "interval": "1d",
        "period1": str(period1),
        "period2": str(period2),
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker, safe='')}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 monthly-box-research/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:250]
        raise BacktestError(f"Yahoo data request for {ticker} failed: HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BacktestError(f"Yahoo data request for {ticker} failed: {exc}") from exc

    try:
        result = payload["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        message = ((payload.get("chart") or {}).get("error") or {}).get("description") if isinstance(payload, dict) else None
        raise BacktestError(f"Yahoo returned no usable data for {ticker}: {message or 'unknown response'}") from exc

    frame = pd.DataFrame({
        "timestamp": result.get("timestamp") or [],
        "open": quote.get("open") or [],
        "high": quote.get("high") or [],
        "low": quote.get("low") or [],
        "close": quote.get("close") or [],
        "volume": quote.get("volume") or [],
    })
    if frame.empty:
        raise BacktestError(f"Yahoo returned an empty history for {ticker}")
    try:
        adjusted = result["indicators"]["adjclose"][0]["adjclose"]
        if len(adjusted) == len(frame):
            frame["adjclose"] = adjusted
    except (KeyError, IndexError, TypeError):
        frame["adjclose"] = np.nan
    if "adjclose" not in frame:
        frame["adjclose"] = np.nan

    frame["date"] = (
        pd.to_datetime(frame["timestamp"], unit="s", utc=True)
        .dt.tz_convert("America/New_York")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"]).sort_values("date")
    frame = frame.drop_duplicates(subset=["date"], keep="last")
    frame = frame.loc[(frame["date"] >= start.tz_localize(None)) & (frame["date"] <= end.tz_localize(None))]
    frame = frame[["date", "open", "high", "low", "close", "volume", "adjclose"]].reset_index(drop=True)
    if frame.empty:
        raise BacktestError(f"Yahoo history for {ticker} contains no complete sessions in requested window")
    return frame


def load_or_fetch_history(
    item: dict[str, str], raw_dir: Path, start: pd.Timestamp, end: pd.Timestamp, no_fetch: bool, retries: int
) -> tuple[dict[str, str], pd.DataFrame | None, str | None, str]:
    ticker = item["ticker"]
    cache = daily_cache_path(raw_dir, ticker)
    if cache.exists() and no_fetch:
        try:
            return item, pd.read_csv(cache, parse_dates=["date"]), None, "cache"
        except Exception as exc:  # cache corruption is a run failure for that ticker
            return item, None, f"cache read failed: {exc}", "cache"
    if no_fetch:
        return item, None, "cache missing with --no-fetch", "cache"

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            history = fetch_yahoo_daily(ticker, start, end)
            temp = cache.with_suffix(".tmp")
            history.to_csv(temp, index=False)
            temp.replace(cache)
            return item, history, None, "yahoo_v8"
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(min(2.0 * attempt, 5.0))
    return item, None, last_error, "yahoo_v8"


def add_features(data: pd.DataFrame) -> pd.DataFrame:
    """Build features available no later than each signal day's close."""
    frame = data.copy().sort_values("date").reset_index(drop=True)
    prior_close = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prior_close).abs(),
        (frame["low"] - prior_close).abs(),
    ], axis=1).max(axis=1)
    frame["atr20"] = true_range.rolling(20, min_periods=20).mean()
    frame["prior_atr20"] = frame["atr20"].shift(1)
    frame["sma50"] = frame["close"].rolling(50, min_periods=50).mean()
    frame["sma200"] = frame["close"].rolling(200, min_periods=200).mean()
    frame["volume20"] = frame["volume"].rolling(20, min_periods=20).mean()
    frame["month"] = frame["date"].dt.to_period("M")
    frame["month_session"] = frame.groupby("month").cumcount() + 1

    completed = frame.groupby("month", sort=True).agg(month_high=("high", "max"), month_low=("low", "min"))
    completed["prior_month_high"] = completed["month_high"].shift(1)
    completed["prior_month_low"] = completed["month_low"].shift(1)
    frame = frame.join(completed[["prior_month_high", "prior_month_low"]], on="month")

    frame["month_open"] = frame.groupby("month")["open"].transform("first")
    frame["month_anchor_atr"] = frame.groupby("month")["prior_atr20"].transform("first")
    frame["low_to_date"] = frame.groupby("month")["low"].cummin()
    frame["ib5_high"] = np.nan
    frame["ib5_low"] = np.nan
    for _, group in frame.groupby("month", sort=True):
        if len(group) >= 5:
            first_five = group.iloc[:5]
            post_initial_balance = group.index[5:]
            frame.loc[post_initial_balance, "ib5_high"] = float(first_five["high"].max())
            frame.loc[post_initial_balance, "ib5_low"] = float(first_five["low"].min())
    return frame


def next_open_same_month(data: pd.DataFrame, index: int) -> bool:
    return index + 1 < len(data) and data.iloc[index + 1]["month"] == data.iloc[index]["month"]


def entry_condition(strategy: str, row: pd.Series, previous: pd.Series | None) -> bool:
    trend = pd.notna(row.sma50) and pd.notna(row.sma200) and row.sma50 > row.sma200
    volume = pd.notna(row.volume20) and row.volume >= row.volume20
    if strategy == "prior_month_high_breakout":
        return bool(trend and volume and pd.notna(row.prior_month_high) and row.close > row.prior_month_high)
    if strategy == "five_day_initial_balance_breakout":
        return bool(trend and volume and row.month_session > 5 and pd.notna(row.ib5_high) and row.close > row.ib5_high)
    if strategy == "monthly_open_reclaim":
        prior = previous.close if previous is not None else np.nan
        pullback_level = row.month_open - row.month_anchor_atr
        saw_pullback = pd.notna(pullback_level) and row.low_to_date <= pullback_level
        crossed_open = pd.notna(prior) and prior <= row.month_open and row.close > row.month_open
        return bool(trend and volume and row.month_session > 1 and saw_pullback and crossed_open)
    raise BacktestError(f"Unknown strategy: {strategy}")


def exit_condition(strategy: str, row: pd.Series) -> bool:
    if strategy == "prior_month_high_breakout":
        return bool(row.close < row.prior_month_high)
    if strategy == "five_day_initial_balance_breakout":
        return bool(row.close < row.ib5_high)
    if strategy == "monthly_open_reclaim":
        return bool(row.close < row.month_open)
    raise BacktestError(f"Unknown strategy: {strategy}")


def find_exit(data: pd.DataFrame, strategy: str, entry_index: int) -> tuple[int | None, str]:
    entry_month = data.iloc[entry_index]["month"]
    for check_index in range(entry_index, len(data) - 1):
        current = data.iloc[check_index]
        following = data.iloc[check_index + 1]
        if exit_condition(strategy, current):
            return check_index + 1, "close_below_rule_level"
        if following.month != entry_month:
            return check_index + 1, "calendar_month_exit"
    return None, "unobserved_exit_at_data_end"


def net_return_from_gross(gross_return: float, round_trip_cost_bps: float) -> float:
    per_side = round_trip_cost_bps / 20_000.0
    return ((1.0 - per_side) / (1.0 + per_side)) * (1.0 + gross_return) - 1.0


def backtest_ticker(data: pd.DataFrame, ticker: str, category: str, cost_bps: float) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    if len(data) <= 202:
        return trades
    for strategy in STRATEGIES:
        index = 200
        while index < len(data) - 1:
            row = data.iloc[index]
            previous = data.iloc[index - 1] if index > 0 else None
            if not next_open_same_month(data, index) or not entry_condition(strategy, row, previous):
                index += 1
                continue
            entry_index = index + 1
            exit_index, reason = find_exit(data, strategy, entry_index)
            if exit_index is None:
                break
            entry = data.iloc[entry_index]
            exit_row = data.iloc[exit_index]
            gross = float(exit_row.open / entry.open - 1.0)
            trades.append({
                "ticker": ticker,
                "category": category,
                "strategy": strategy,
                "signal_date": row.date.date().isoformat(),
                "entry_date": entry.date.date().isoformat(),
                "entry_price": float(entry.open),
                "exit_date": exit_row.date.date().isoformat(),
                "exit_price": float(exit_row.open),
                "exit_reason": reason,
                "holding_calendar_days": int((exit_row.date - entry.date).days),
                "gross_return": gross,
                "net_return": net_return_from_gross(gross, cost_bps),
                "round_trip_cost_bps": float(cost_bps),
                "signal_sma50": float(row.sma50),
                "signal_sma200": float(row.sma200),
                "signal_volume_vs_20d": float(row.volume / row.volume20),
            })
            index = exit_index
    return trades


def empty_metrics() -> dict[str, Any]:
    return {
        "trades": 0, "tickers": 0, "win_rate": np.nan, "mean_net_return": np.nan,
        "median_net_return": np.nan, "mean_gross_return": np.nan, "profit_factor": np.nan,
        "net_return_sum": np.nan, "average_holding_days": np.nan,
    }


def trade_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return empty_metrics()
    net = trades["net_return"].astype(float)
    gross = trades["gross_return"].astype(float)
    gains = net[net > 0].sum()
    losses = -net[net < 0].sum()
    return {
        "trades": int(len(trades)),
        "tickers": int(trades["ticker"].nunique()),
        "win_rate": float((net > 0).mean()),
        "mean_net_return": float(net.mean()),
        "median_net_return": float(net.median()),
        "mean_gross_return": float(gross.mean()),
        "profit_factor": float(gains / losses) if losses > 0 else np.nan,
        "net_return_sum": float(net.sum()),
        "average_holding_days": float(trades["holding_calendar_days"].mean()),
    }


def ticker_cluster_bootstrap_ci(trades: pd.DataFrame, n_bootstrap: int, seed: int) -> tuple[float, float]:
    if trades.empty or trades["ticker"].nunique() < 5:
        return np.nan, np.nan
    groups = [group["net_return"].to_numpy(dtype=float) for _, group in trades.groupby("ticker", sort=True)]
    rng = np.random.default_rng(seed)
    means = np.empty(n_bootstrap, dtype=float)
    for index in range(n_bootstrap):
        selected = rng.integers(0, len(groups), size=len(groups))
        means[index] = np.concatenate([groups[item] for item in selected]).mean()
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def ticker_concentration(trades: pd.DataFrame) -> float:
    if trades.empty:
        return np.nan
    pnl = trades.groupby("ticker")["net_return"].sum().abs()
    total = float(pnl.sum())
    return float(pnl.max() / total) if total > 0 else np.nan


def category_concentration(trades: pd.DataFrame) -> float:
    if trades.empty:
        return np.nan
    pnl = trades.groupby("category")["net_return"].sum().abs()
    total = float(pnl.sum())
    return float(pnl.max() / total) if total > 0 else np.nan


def split_samples(trades: pd.DataFrame, holdout_start: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return development, untouched-holdout, and boundary-purged trades.

    A trade enters development only when its signal, fill, and exit all precede
    the first full holdout month. A holdout trade must have both signal and
    fill inside that holdout. Every cross-boundary trade is removed rather than
    allowing a development signal to consume holdout information.
    """
    if trades.empty:
        return trades.copy(), trades.copy(), trades.copy()
    signal_dates = pd.to_datetime(trades["signal_date"])
    entry_dates = pd.to_datetime(trades["entry_date"])
    exit_dates = pd.to_datetime(trades["exit_date"])
    development_mask = (signal_dates < holdout_start) & (entry_dates < holdout_start) & (exit_dates < holdout_start)
    holdout_mask = (signal_dates >= holdout_start) & (entry_dates >= holdout_start) & (exit_dates >= holdout_start)
    purged_mask = ~(development_mask | holdout_mask)
    return trades.loc[development_mask].copy(), trades.loc[holdout_mask].copy(), trades.loc[purged_mask].copy()


def category_metrics(trades: pd.DataFrame, holdout_start: pd.Timestamp, categories: tuple[str, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    development, holdout, _ = split_samples(trades, holdout_start)
    for sample, subset in (("development", development), ("holdout", holdout)):
        for strategy in STRATEGIES:
            for category in categories:
                metrics = trade_metrics(subset[(subset["strategy"] == strategy) & (subset["category"] == category)])
                rows.append({"sample": sample, "strategy": strategy, "strategy_label": STRATEGY_LABELS[strategy], "category": category, **metrics})
    return pd.DataFrame(rows)


def ticker_metrics(trades: pd.DataFrame, holdout_start: pd.Timestamp) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    development, holdout, _ = split_samples(trades, holdout_start)
    for sample, subset in (("development", development), ("holdout", holdout)):
        for (ticker, category, strategy), group in subset.groupby(["ticker", "category", "strategy"], sort=True):
            rows.append({"sample": sample, "ticker": ticker, "category": category, "strategy": strategy, "strategy_label": STRATEGY_LABELS[strategy], **trade_metrics(group)})
    return pd.DataFrame(rows)


def validation_scorecard(
    trades: pd.DataFrame,
    holdout_start: pd.Timestamp,
    holdout_end: pd.Timestamp,
    categories: tuple[str, ...],
    cost_bps: float,
    n_bootstrap: int,
    induction_config_fixed: bool,
    independent_review_attached: bool,
    point_in_time_universe_available: bool,
    complete_history_coverage: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    development, holdout, purged = split_samples(trades, holdout_start)
    midpoint = holdout_start + (holdout_end - holdout_start) / 2
    score_rows: list[dict[str, Any]] = []
    validation: dict[str, Any] = {
        "method": "predeclared monthly-box rules; ticker-cluster bootstrap; no parameter search; full-month holdout boundary purge",
        "boundary_purged_trades": int(len(purged)),
        "eligibility_context": {
            "induction_config_fixed": induction_config_fixed,
            "independent_review_attached": independent_review_attached,
            "point_in_time_universe_available": point_in_time_universe_available,
            "complete_history_coverage": complete_history_coverage,
        },
        "strategies": {},
    }

    for seed, strategy in enumerate(STRATEGIES, start=101):
        dev = development[development["strategy"] == strategy].copy()
        ho = holdout[holdout["strategy"] == strategy].copy()
        dev_metrics = trade_metrics(dev)
        ho_metrics = trade_metrics(ho)
        ci_low, ci_high = ticker_cluster_bootstrap_ci(ho, n_bootstrap, seed)
        mean_at_40 = np.nan if ho.empty else float(ho["gross_return"].map(lambda value: net_return_from_gross(value, 40.0)).mean())
        ticker_share = ticker_concentration(ho)
        category_share = category_concentration(ho)
        first_half = ho[pd.to_datetime(ho["entry_date"]) < midpoint]
        second_half = ho[pd.to_datetime(ho["entry_date"]) >= midpoint]
        first_half_mean = trade_metrics(first_half)["mean_net_return"]
        second_half_mean = trade_metrics(second_half)["mean_net_return"]

        category_gate_details: dict[str, Any] = {}
        category_pass = True
        for category in categories:
            subset = ho[ho["category"] == category]
            metrics = trade_metrics(subset)
            required = metrics["trades"] >= 10
            passed = bool(required and metrics["mean_net_return"] > 0)
            category_gate_details[category] = {"trades": metrics["trades"], "mean_net_return": metrics["mean_net_return"], "required": required, "passed": passed}
            category_pass = category_pass and passed

        gates = {
            "g1_observations_and_tickers": bool(ho_metrics["trades"] >= 50 and ho_metrics["tickers"] >= 10),
            "g2_holdout_mean_and_median_positive": bool(ho_metrics["mean_net_return"] > 0 and ho_metrics["median_net_return"] > 0),
            "g3_ticker_cluster_ci_positive": bool(pd.notna(ci_low) and ci_low > 0),
            "g4_development_mean_positive": bool(dev_metrics["mean_net_return"] > 0),
            "g5_category_consistency": bool(category_pass),
            "g6_cost_40bps_positive": bool(pd.notna(mean_at_40) and mean_at_40 > 0),
            "g7_ticker_concentration_lte_25pct": bool(pd.notna(ticker_share) and ticker_share <= MAX_TICKER_CONCENTRATION),
            "g8_holdout_halves_positive": bool(
                len(first_half) >= 10 and len(second_half) >= 10 and first_half_mean > 0 and second_half_mean > 0
            ),
            "g9_category_concentration_lte_60pct": bool(pd.notna(category_share) and category_share <= MAX_CATEGORY_CONCENTRATION),
            "g10_fixed_induction_configuration": induction_config_fixed,
            "g11_independent_review_attached": independent_review_attached,
            "g12_point_in_time_universe_available": point_in_time_universe_available,
            "g13_complete_history_coverage": complete_history_coverage,
        }
        eligible = all(gates.values())
        record = {
            "strategy": strategy,
            "strategy_label": STRATEGY_LABELS[strategy],
            "development_trades": dev_metrics["trades"],
            "development_tickers": dev_metrics["tickers"],
            "development_mean_net_return": dev_metrics["mean_net_return"],
            "holdout_trades": ho_metrics["trades"],
            "holdout_tickers": ho_metrics["tickers"],
            "holdout_win_rate": ho_metrics["win_rate"],
            "holdout_mean_net_return": ho_metrics["mean_net_return"],
            "holdout_median_net_return": ho_metrics["median_net_return"],
            "holdout_profit_factor": ho_metrics["profit_factor"],
            "holdout_ci95_low": ci_low,
            "holdout_ci95_high": ci_high,
            "holdout_mean_net_return_40bps": mean_at_40,
            "holdout_ticker_concentration": ticker_share,
            "holdout_category_concentration": category_share,
            "holdout_first_half_mean": first_half_mean,
            "holdout_second_half_mean": second_half_mean,
            "boundary_purged_trades": len(purged),
            "round_trip_cost_bps": cost_bps,
            "induction_review_eligible": eligible,
            **gates,
        }
        score_rows.append(record)
        validation["strategies"][strategy] = {
            "label": STRATEGY_LABELS[strategy],
            "induction_review_eligible": eligible,
            "gates": gates,
            "category_gate_details": category_gate_details,
            "holdout_metrics": ho_metrics,
            "development_metrics": dev_metrics,
            "holdout_ci95": {"lower": ci_low, "upper": ci_high, "bootstrap": "ticker-cluster", "draws": n_bootstrap},
            "cost_sensitivity": {"round_trip_cost_bps": 40, "holdout_mean_net_return": mean_at_40},
            "concentration": {"max_abs_ticker_pnl_share": ticker_share, "max_abs_category_pnl_share": category_share},
            "holdout_halves": {"first_trades": len(first_half), "first_mean": first_half_mean, "second_trades": len(second_half), "second_mean": second_half_mean},
        }
    validation["overall_verdict"] = "INDUCTION REVIEW ELIGIBLE" if any(row["induction_review_eligible"] for row in score_rows) else "DO NOT INDUCT"
    return pd.DataFrame(score_rows), validation


def load_independent_review(
    path: Path | None, run_name: str, code_sha256: str, config_sha256: str, run_operator_id: str
) -> tuple[bool, dict[str, Any]]:
    """Validate a hash-bound review attestation from a distinct declared agent."""
    status: dict[str, Any] = {"attached": False, "eligible": False, "path": None, "run_operator_id": run_operator_id}
    if path is None:
        return False, status
    if not path.exists():
        raise BacktestError(f"independent review file does not exist: {path}")
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestError(f"independent review file is not valid JSON: {exc}") from exc
    if not isinstance(review, dict):
        raise BacktestError("independent review must be a JSON object")
    for key, expected in (
        ("run_name", run_name), ("code_sha256", code_sha256), ("config_sha256", config_sha256),
        ("run_operator_id", run_operator_id),
    ):
        if review.get(key) != expected:
            raise BacktestError(f"independent review {key} does not match this run")
    if not review.get("reviewed_utc"):
        raise BacktestError("independent review must identify reviewer_agent_id and reviewed_utc")
    reviewer_agent_id = canonical_agent_id(review.get("reviewer_agent_id"), "reviewer_agent_id")
    if reviewer_agent_id == run_operator_id:
        raise BacktestError("independent review must use a reviewer_agent_id different from run_operator_id")
    eligible = review.get("verdict") == "INDUCTION REVIEW ELIGIBLE"
    status = {
        "attached": True,
        "eligible": eligible,
        "path": str(path.resolve()),
        "run_operator_id": run_operator_id,
        "reviewer_agent_id": reviewer_agent_id,
        "reviewed_utc": str(review["reviewed_utc"]),
        "verdict": str(review.get("verdict") or ""),
        "sha256": file_sha256(path),
    }
    return eligible, status


def clean_json(value: Any) -> Any:
    """Convert pandas/numpy values and non-finite metrics to portable JSON."""
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if isinstance(value, (np.floating, np.integer)):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def fmt_pct(value: Any) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):.2%}"


def fmt_num(value: Any) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):.3f}"


def write_report(
    output: Path,
    manifest: dict[str, Any],
    scorecard: pd.DataFrame,
    categories: pd.DataFrame,
    validation: dict[str, Any],
) -> None:
    accepted = manifest["watchlist"]["accepted"]
    failures = manifest["fetch_failures"]
    lines = [
        "# Monthly-Box Backtest Report", "",
        "## Scope", "",
        f"- **Generated:** {manifest['generated_utc']}",
        f"- **Source:** Yahoo v8 daily OHLCV; **watchlist source:** {manifest['watchlist']['source']}",
        f"- **Universe:** {len(accepted)} accepted explicitly US-listed symbols from `core`, `satellite`, and `watch1`; {len(failures)} history failures.",
        f"- **History / holdout:** {manifest['parameters']['years']} years through {manifest['parameters']['as_of']}; untouched holdout starts {manifest['parameters']['holdout_start']}.",
        f"- **Execution:** signal at close, entry/exit at next session open; fixed {manifest['parameters']['round_trip_cost_bps']:.0f} bps round trip.",
        f"- **Price basis:** {manifest['price_basis']}; {len(manifest['corporate_action_events'])} material adjustment-factor changes flagged for audit.",
        f"- **Session continuity:** {len(manifest['permitted_single_weekday_gaps'])} isolated weekday gaps recorded in `data_gaps.csv`; any gap of two or more weekdays fails the run.",
        f"- **Boundary purge:** {validation['boundary_purged_trades']} trades touching the development/holdout boundary were excluded from both samples.",
        "- **Pre-registration:** exactly three fixed rules; no parameter grid search or selection on the holdout.",
        "",
        "## Look-ahead control", "",
        "> The final high/low of a current month is unknown during that month. The runner uses only the **prior completed month’s** range, the first five completed sessions, or the month-open anchor. A signal is filled strictly at the next open and never if that open crosses a calendar-month boundary.",
        "",
        "## Induction scorecard", "",
        "| Strategy | Dev trades | Holdout trades | Holdout tickers | Mean net / trade | Median | 95% cluster CI lower | 40 bps mean | Ticker / category concentration | Eligible |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in scorecard.itertuples(index=False):
        lines.append(
            f"| {row.strategy_label} | {row.development_trades} | {row.holdout_trades} | {row.holdout_tickers} | {fmt_pct(row.holdout_mean_net_return)} | {fmt_pct(row.holdout_median_net_return)} | {fmt_pct(row.holdout_ci95_low)} | {fmt_pct(row.holdout_mean_net_return_40bps)} | {fmt_pct(row.holdout_ticker_concentration)} / {fmt_pct(row.holdout_category_concentration)} | {'YES' if row.induction_review_eligible else 'NO'} |"
        )
    lines.extend(["", "## Holdout category aggregates", "", "| Strategy | Category | Trades | Tickers | Mean net / trade | Median | Win rate |", "|---|---|---:|---:|---:|---:|---:|"])
    if not categories.empty:
        filtered = categories[categories["sample"] == "holdout"]
        for row in filtered.itertuples(index=False):
            lines.append(
                f"| {row.strategy_label} | {row.category} | {row.trades} | {row.tickers} | {fmt_pct(row.mean_net_return)} | {fmt_pct(row.median_net_return)} | {fmt_pct(row.win_rate)} |"
            )
    lines.extend([
        "",
        "## Verdict", "",
        f"**{validation['overall_verdict']}** — this is a formal research gate, not a trade recommendation. A separate AI agent must audit the raw artifacts before any governance decision.",
        "",
        "## Required artifacts", "",
        "- `manifest.json` — accepted/rejected universe and fetch exceptions",
        "- `trades.csv` and `boundary_purged_trades.csv` — signal/next-open/exit and boundary-exclusion audit trails",
        "- `corporate_actions.csv` — material Yahoo adjustment-factor discontinuities to verify",
        "- `data_gaps.csv` — permitted single-weekday source gaps; multi-day weekday gaps fail the run",
        "- `ticker_results.csv`, `category_results.csv`, and `scorecard.csv` — all metric tables",
        "- `validation.json` — gate evidence and machine-readable verdict",
        "",
        "## Disclosure", "",
        "- **Basis:** Daily OHLCV trade-rule research; prices and signals are not personalized recommendations.",
        f"- **Time:** Data is requested through {manifest['parameters']['as_of']} and the holdout starts {manifest['parameters']['holdout_start']}.",
        "- **Assumptions:** Next-open execution, fixed transaction costs, no taxes/market impact/borrow costs, and no parameter optimization. Yahoo adjusted-close factors are applied consistently to OHLC to remove split/ex-dividend discontinuities; broker-quality executable prices remain required before deployment.",
        "- **Survivorship limitation:** The supplied live watchlist is not point-in-time historical membership. This run is therefore exploratory and cannot be inducted until a dated historical-universe source is supplied and reviewed.",
        "- **Compliance:** This is research and analysis only, not personalized financial advice.",
    ])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="New or resumable run directory")
    parser.add_argument("--watchlist-file", type=Path, help="Reviewed CSV/JSON override; otherwise read Supabase")
    parser.add_argument("--env-file", type=Path, help="Optional local env file; never commit it")
    parser.add_argument("--watchlist-table", default="watchlist")
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    parser.add_argument("--max-symbols", type=int, default=500, help="Fail rather than silently truncate if the accepted universe exceeds this limit")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--holdout-years", type=int, default=2)
    parser.add_argument("--as-of", help="YYYY-MM-DD, defaults to today UTC")
    parser.add_argument("--round-trip-cost-bps", type=float, default=20.0)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--bootstrap-draws", type=int, default=2000)
    parser.add_argument("--no-fetch", action="store_true", help="Use only cached CSVs in output-dir/raw")
    parser.add_argument("--exploratory", action="store_true", help="Permit non-induction history/cost settings; verdict remains DO NOT INDUCT")
    parser.add_argument("--independent-review-file", type=Path, help="Separate reviewer JSON bound to this run's code and configuration hashes")
    parser.add_argument("--run-operator-id", default=os.environ.get("MONTHLY_BOX_RUN_OPERATOR_ID", ""), help="Canonical identifier of the agent that ran the study; required for every run")
    args = parser.parse_args()

    if args.years < 5 or args.holdout_years < 1 or args.holdout_years >= args.years:
        raise BacktestError("Use at least five years of history and a holdout shorter than the full history")
    if args.round_trip_cost_bps < 0 or args.max_workers < 1 or args.max_symbols < 1:
        raise BacktestError("Costs must be non-negative; worker and symbol limits must be positive")

    categories = tuple(item.strip().lower() for item in args.categories.split(",") if item.strip())
    if categories != DEFAULT_CATEGORIES:
        raise BacktestError("This skill requires exactly the ordered categories core,satellite,watch1")
    induction_config_fixed = bool(
        args.years == INDUCTION_HISTORY_YEARS
        and args.holdout_years == INDUCTION_HOLDOUT_YEARS
        and abs(args.round_trip_cost_bps - INDUCTION_COST_BPS) < 1e-12
        and args.bootstrap_draws == INDUCTION_BOOTSTRAP_DRAWS
        and not args.exploratory
    )
    if not induction_config_fixed and not args.exploratory:
        raise BacktestError("Induction configuration is fixed at 10 years, a two-year holdout, 20 bps round-trip cost, and 2,000 bootstrap draws; use --exploratory for other settings")
    raw_operator_id = args.run_operator_id
    run_operator_id = canonical_agent_id(raw_operator_id, "run_operator_id") if raw_operator_id else ""
    if not run_operator_id:
        raise BacktestError("--run-operator-id or MONTHLY_BOX_RUN_OPERATOR_ID is required for every run")
    as_of = pd.Timestamp(args.as_of or datetime.now(timezone.utc).date().isoformat()).normalize()
    history_start = as_of - pd.DateOffset(years=args.years)
    holdout_start = (as_of - pd.DateOffset(years=args.holdout_years)).to_period("M").start_time

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    if args.watchlist_file:
        raw_watchlist = load_watchlist_file(args.watchlist_file)
        watchlist_source = str(args.watchlist_file.resolve())
    else:
        base_url, key = resolve_supabase_credentials(args.env_file)
        raw_watchlist = load_watchlist_supabase(base_url, key, args.watchlist_table, categories)
        watchlist_source = f"Supabase:{args.watchlist_table}"

    accepted, rejected = prepare_watchlist(raw_watchlist, categories, args.max_symbols)
    if not accepted:
        raise BacktestError("No explicitly US-listed symbols remain after category filtering")
    if len(accepted) > args.max_symbols:
        raise BacktestError(f"Accepted universe has {len(accepted)} symbols, exceeding --max-symbols={args.max_symbols}; do not truncate an induction study")
    accepted_by_category = {category: sum(item["category"] == category for item in accepted) for category in categories}
    missing_accepted_categories = [category for category, count in accepted_by_category.items() if count == 0]
    if missing_accepted_categories:
        raise BacktestError(f"Accepted US watchlist lacks required categories: {','.join(missing_accepted_categories)}")

    histories: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    cache_hashes: dict[str, str] = {}
    corporate_action_events: list[dict[str, Any]] = []
    permitted_data_gaps: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        pending = [
            pool.submit(load_or_fetch_history, item, raw_dir, history_start, as_of, args.no_fetch, args.retries)
            for item in accepted
        ]
        for future in concurrent.futures.as_completed(pending):
            item, history, error, source = future.result()
            if error:
                failures.append({"ticker": item["ticker"], "category": item["category"], "error": error})
                continue
            assert history is not None
            try:
                raw_history, gaps = validate_raw_history(history, history_start, as_of)
                history, events = continuous_price_basis(raw_history)
            except BacktestError as exc:
                failures.append({"ticker": item["ticker"], "category": item["category"], "error": str(exc)})
                continue
            if len(history) < 240 * args.years:
                failures.append({"ticker": item["ticker"], "category": item["category"], "error": f"insufficient session count: {len(history)}"})
                continue
            histories[item["ticker"]] = history
            sources[item["ticker"]] = source
            cache_hashes[item["ticker"]] = file_sha256(daily_cache_path(raw_dir, item["ticker"]))
            permitted_data_gaps.extend({"ticker": item["ticker"], "category": item["category"], **gap} for gap in gaps)
            for event in events.itertuples(index=False):
                corporate_action_events.append({
                    "ticker": item["ticker"], "category": item["category"], "date": event.date.date().isoformat(),
                    "factor_change": float(event.factor_change), "adjustment_factor": float(event.adjustment_factor),
                    "raw_close": float(event.raw_close), "adjusted_close": float(event.close),
                })

    if not histories:
        raise BacktestError("No ticker histories passed minimum-history checks")
    loaded_by_category = {
        category: sum(item["category"] == category and item["ticker"] in histories for item in accepted)
        for category in categories
    }
    missing_loaded_categories = [category for category, count in loaded_by_category.items() if count == 0]
    if missing_loaded_categories:
        raise BacktestError(f"No complete history loaded for required categories: {','.join(missing_loaded_categories)}")
    complete_history_coverage = accepted_by_category == loaded_by_category

    all_trades: list[dict[str, Any]] = []
    for item in accepted:
        history = histories.get(item["ticker"])
        if history is None:
            continue
        all_trades.extend(backtest_ticker(add_features(history), item["ticker"], item["category"], args.round_trip_cost_bps))

    columns = [
        "ticker", "category", "strategy", "signal_date", "entry_date", "entry_price", "exit_date", "exit_price",
        "exit_reason", "holding_calendar_days", "gross_return", "net_return", "round_trip_cost_bps",
        "signal_sma50", "signal_sma200", "signal_volume_vs_20d",
    ]
    trades = pd.DataFrame(all_trades, columns=columns)
    if not trades.empty:
        trades = trades.sort_values(["strategy", "ticker", "entry_date"]).reset_index(drop=True)
    trades.to_csv(output_dir / "trades.csv", index=False, float_format="%.10f")

    development_trades, holdout_trades, boundary_purged_trades = split_samples(trades, holdout_start)
    boundary_purged_trades.to_csv(output_dir / "boundary_purged_trades.csv", index=False, float_format="%.10f")
    corporate_actions = pd.DataFrame(corporate_action_events, columns=[
        "ticker", "category", "date", "factor_change", "adjustment_factor", "raw_close", "adjusted_close",
    ])
    corporate_actions.to_csv(output_dir / "corporate_actions.csv", index=False, float_format="%.10f")
    data_gaps = pd.DataFrame(permitted_data_gaps, columns=["ticker", "category", "after_date", "before_date", "missing_weekday"])
    data_gaps.to_csv(output_dir / "data_gaps.csv", index=False)

    ticker_results = ticker_metrics(trades, holdout_start)
    if ticker_results.empty:
        ticker_results = pd.DataFrame(columns=[
            "sample", "ticker", "category", "strategy", "strategy_label", *empty_metrics().keys(),
        ])
    category_results = category_metrics(trades, holdout_start, categories)
    ticker_results.to_csv(output_dir / "ticker_results.csv", index=False, float_format="%.10f")
    category_results.to_csv(output_dir / "category_results.csv", index=False, float_format="%.10f")

    code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    config_payload = {
        "run_name": output_dir.name,
        "as_of": as_of.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "holdout_start": holdout_start.date().isoformat(),
        "years": args.years,
        "holdout_years": args.holdout_years,
        "round_trip_cost_bps": args.round_trip_cost_bps,
        "bootstrap_draws": args.bootstrap_draws,
        "exploratory": args.exploratory,
        "max_symbols": args.max_symbols,
        "run_operator_id": run_operator_id,
        "accepted_universe": [{key: item[key] for key in ("ticker", "category", "exchange")} for item in accepted],
        "raw_cache_sha256": dict(sorted(cache_hashes.items())),
        "price_basis": "Yahoo adjclose/close factor applied consistently to OHLC",
    }
    config_sha256 = hashlib.sha256(json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    independent_review_attached, review_status = load_independent_review(
        args.independent_review_file, output_dir.name, code_sha256, config_sha256, run_operator_id
    )
    scorecard, validation = validation_scorecard(
        trades, holdout_start, as_of, categories, args.round_trip_cost_bps, args.bootstrap_draws,
        induction_config_fixed, independent_review_attached, False, complete_history_coverage,
    )
    scorecard.to_csv(output_dir / "scorecard.csv", index=False, float_format="%.10f")

    manifest = {
        "generated_utc": utc_now(),
        "run_name": output_dir.name,
        "run_operator_id": run_operator_id,
        "code_sha256": code_sha256,
        "config_sha256": config_sha256,
        "config": config_payload,
        "provider": "Yahoo v8 chart API",
        "price_basis": "Yahoo adjclose/close factor applied consistently to OHLC; material factor changes appear in corporate_actions.csv",
        "runtime": {"python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__},
        "parameters": {
            "as_of": as_of.date().isoformat(),
            "history_start": history_start.date().isoformat(),
            "holdout_start": holdout_start.date().isoformat(),
            "years": args.years,
            "holdout_years": args.holdout_years,
            "round_trip_cost_bps": args.round_trip_cost_bps,
            "bootstrap_draws": args.bootstrap_draws,
            "exploratory": args.exploratory,
        },
        "watchlist": {
            "source": watchlist_source, "raw_rows": len(raw_watchlist), "accepted": accepted, "rejected": rejected,
            "accepted_by_category": accepted_by_category, "loaded_by_category": loaded_by_category,
            "complete_history_coverage": complete_history_coverage,
            "point_in_time_historical_membership": False,
        },
        "histories_loaded": {
            ticker: {
                "source": sources[ticker], "sessions": len(history), "first": history.iloc[0].date.date().isoformat(),
                "last": history.iloc[-1].date.date().isoformat(), "raw_cache_sha256": cache_hashes[ticker],
            }
            for ticker, history in histories.items()
        },
        "corporate_action_events": sorted(corporate_action_events, key=lambda row: (row["ticker"], row["date"])),
        "permitted_single_weekday_gaps": sorted(permitted_data_gaps, key=lambda row: (row["ticker"], row["missing_weekday"])),
        "fetch_failures": sorted(failures, key=lambda row: (row["category"], row["ticker"])),
        "independent_review": review_status,
        "induction_limitations": [
            "Current watchlist is not a point-in-time historical universe; induction remains blocked.",
            "Broker-quality executable prices and corporate-action verification remain required before deployment.",
        ],
    }
    validation["reproducibility"] = {
        "run_name": output_dir.name, "code_sha256": code_sha256, "config_sha256": config_sha256,
        "boundary_purged_trades": len(boundary_purged_trades), "independent_review": review_status,
    }
    (output_dir / "manifest.json").write_text(json.dumps(clean_json(manifest), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "validation.json").write_text(json.dumps(clean_json(validation), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_report(output_dir / "report.md", manifest, scorecard, category_results, validation)

    print(f"Accepted US watchlist symbols: {len(accepted)}; histories loaded: {len(histories)}; failures: {len(failures)}")
    print(f"Completed trades: {len(trades)}; verdict: {validation['overall_verdict']}")
    print(f"Run directory: {output_dir}")
    print("This is research and analysis only, not personalized financial advice.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BacktestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
