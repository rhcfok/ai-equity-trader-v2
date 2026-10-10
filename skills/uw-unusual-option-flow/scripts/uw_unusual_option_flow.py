#!/usr/bin/env python3
"""Run the unusual-option-flow workflow from Unusual Whales Full Tape archives.

The runner replaces the ORATS daily-strike provider with Unusual Whales'
transaction-level Full Tape.  It downloads only four completed US trading-day
archives (event day plus three prior sessions), retains only configured
US-listed watchlist symbols during parsing, produces a JSON audit artifact,
and writes to Supabase only when --write-supabase is explicit.

No orders are placed and no brokerage is accessed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MODEL_VERSION = "Unusual Whales Full Tape Unusual Option Flow v1.0"
UW_BASE_URL = "https://api.unusualwhales.com"
DEFAULT_WATCHLIST_TABLE = "watchlist"
DEFAULT_SIGNAL_TABLE = "option_flow_symbol_signals"
DEFAULT_POSITION_ACTIONS_TABLE = "orats_position_actions_daily"
DEFAULT_CATEGORIES = ("core", "satellite", "watch1")
DEFAULT_CACHE_DIR = Path("uw_unusual_option_flow_cache")
EASTERN = ZoneInfo("America/New_York")
US_EXCHANGES = {
    "NASDAQ", "NASDAQGS", "NASDAQCM", "NASDAQGM", "NYSE", "NYSEARCA",
    "NYSE AMERICAN", "NYSE MKT", "AMEX", "ARCA", "BATS", "CBOE", "IEX",
    "US", "USA",
}


class RunnerError(RuntimeError):
    """Raised for invalid configuration, source data, or persistence failures."""


@dataclass(frozen=True)
class FlowConfig:
    lookback_days: int = 3
    max_symbols: int = 200
    max_candidates_per_symbol: int = 25
    min_volume: int = 500
    min_premium: float = 1_000_000.0
    min_dte: int = 0
    max_dte: int = 180
    min_unusual: int = 1
    min_unusual_premium: float = 2_000_000.0
    iv_rise_threshold: float = 0.02
    iv_fall_threshold: float = -0.02
    iv_rise_multiplier: float = 1.35
    iv_fall_multiplier: float = 0.50
    bias_net_strong: float = 0.60
    top_trades: int = 12


@dataclass
class DailyContractAggregate:
    ticker: str
    option_chain_id: str
    expiry: str
    option_type: str
    strike: float
    volume: float = 0.0
    premium: float = 0.0
    trade_count: int = 0
    last_executed_at: str = ""
    implied_volatility: float | None = None
    delta: float | None = None
    open_interest: float | None = None
    underlying_price: float | None = None


@dataclass
class FlowReport:
    run_id: str
    model_version: str
    started_at: str
    event_date: str
    prior_dates: list[str]
    input_symbols: int = 0
    accepted_us_symbols: int = 0
    processed_symbols: int = 0
    signal_count: int = 0
    skipped_symbols: list[dict[str, str]] = field(default_factory=list)
    failed_symbols: list[dict[str, str]] = field(default_factory=list)
    tape_sources: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    supabase: dict[str, Any] = field(default_factory=dict)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=os.sys.stderr,
    )


def load_dotenv(path: Path) -> None:
    """Load a simple dotenv file without replacing inherited environment values."""
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


def num(value: Any, fallback: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("$")


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"Invalid ISO date: {value!r}") from exc


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST", "PATCH")),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "UWUnusualOptionFlow/1.0", "Accept": "application/json"})
    return session


# NYSE regular-session calendar.  Unscheduled closures are intentionally not
# invented; pin --event-date when a non-standard closure is relevant.
def easter_sunday(year: int) -> date:
    """Return Gregorian Easter using the Meeus/Jones/Butcher computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    candidate = date(year, month, day)
    if candidate.weekday() == 5:
        return candidate - timedelta(days=1)
    if candidate.weekday() == 6:
        return candidate + timedelta(days=1)
    return candidate


def nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    cursor = date(year, month, 1)
    cursor += timedelta(days=(weekday - cursor.weekday()) % 7 + 7 * (occurrence - 1))
    return cursor


def last_weekday(year: int, month: int, weekday: int) -> date:
    cursor = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def nyse_holidays(year: int) -> set[date]:
    """Return planned full-day NYSE holidays for the supplied year."""
    return {
        observed_fixed_holiday(year, 1, 1),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed_fixed_holiday(year, 6, 19),
        observed_fixed_holiday(year, 7, 4),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_fixed_holiday(year, 12, 25),
    }


def is_trading_day(value: date) -> bool:
    return value.weekday() < 5 and value not in nyse_holidays(value.year)


def previous_trading_day(value: date) -> date:
    cursor = value - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def default_event_date() -> date:
    """Select the most recently completed regular US market session."""
    now = datetime.now(EASTERN)
    today = now.date()
    if is_trading_day(today) and now.timetz().replace(tzinfo=None) >= clock_time(17, 30):
        return today
    return previous_trading_day(today)


def dates_for_run(event_date_value: str | None, lookback_days: int) -> tuple[str, list[str]]:
    if lookback_days < 1:
        raise RunnerError("lookback-days must be at least one")
    event = parse_iso_date(event_date_value) if event_date_value else default_event_date()
    if not is_trading_day(event):
        raise RunnerError("event-date must be a planned NYSE trading day")
    priors: list[str] = []
    cursor = event
    for _ in range(lookback_days):
        cursor = previous_trading_day(cursor)
        priors.append(cursor.isoformat())
    return event.isoformat(), priors


def is_us_listing(row: dict[str, Any]) -> bool:
    """Require an explicit US venue/country to avoid silently including non-US names."""
    venue = str(
        row.get("exchange")
        or row.get("primary_exchange")
        or row.get("mic")
        or row.get("country")
        or ""
    ).strip().upper()
    return venue in US_EXCHANGES or venue.startswith("NASDAQ") or venue.startswith("NYSE")


def load_watchlist_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RunnerError(f"Watchlist file does not exist: {path}")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return payload
    raise RunnerError("Watchlist must be a CSV or a JSON array of objects")


def supabase_headers(key: str, prefer: str | None = None) -> dict[str, str]:
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_table_url(base_url: str, table: str) -> str:
    base = base_url.rstrip("/")
    if not base:
        raise RunnerError("SUPABASE_URL is required")
    if not base.endswith("/rest/v1"):
        base = f"{base}/rest/v1"
    return f"{base}/{table}"


def load_watchlist_supabase(
    session: requests.Session, base_url: str, key: str, table: str, categories: tuple[str, ...]
) -> list[dict[str, Any]]:
    response = session.get(
        supabase_table_url(base_url, table),
        headers=supabase_headers(key),
        params={
            "select": "id,symbol,ticker,exchange,primary_exchange,mic,country,category",
            "category": "in.(" + ",".join(categories) + ")",
            "limit": "2000",
        },
        timeout=45,
    )
    if not response.ok:
        raise RunnerError(f"Supabase watchlist read failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise RunnerError("Supabase watchlist response was not a JSON array")
    return payload


def prepare_watchlist(
    rows: Iterable[dict[str, Any]], categories: tuple[str, ...], max_symbols: int, report: FlowReport
) -> list[dict[str, str]]:
    """Apply category order, US-exchange restriction, and ticker de-duplication."""
    raw_rows = list(rows)
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for category in categories:
        for row in raw_rows:
            if str(row.get("category") or "").strip() != category:
                continue
            ticker = normalize_symbol(row.get("symbol") or row.get("ticker"))
            if not ticker or ticker in seen:
                continue
            if not is_us_listing(row):
                report.skipped_symbols.append({"symbol": ticker, "reason": "not explicitly identified as a US listing"})
                seen.add(ticker)
                continue
            seen.add(ticker)
            output.append({"ticker": ticker, "category": category})
            if len(output) >= max_symbols:
                return output
    return output


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def normalize_option_type(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"call", "c"}:
        return "call"
    if normalized in {"put", "p"}:
        return "put"
    return None


class UnusualWhalesFullTapeClient:
    """Download and reduce UW Full Tape archives to requested contract-day aggregates."""

    def __init__(
        self, session: requests.Session, api_key: str, cache_dir: Path, base_url: str, client_api_id: str, keep_archives: bool
    ) -> None:
        if not api_key:
            raise RunnerError("UW_API_KEY or UNUSUAL_WHALES_API_KEY is required")
        self.session = session
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.base_url = base_url.rstrip("/")
        self.client_api_id = client_api_id
        self.keep_archives = keep_archives
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "aggregates").mkdir(exist_ok=True)
        if self.keep_archives:
            (self.cache_dir / "archives").mkdir(exist_ok=True)

    def _cache_path(self, trade_date: str) -> Path:
        return self.cache_dir / "aggregates" / f"{trade_date}.json"

    def _load_cache(self, trade_date: str, symbols: set[str]) -> tuple[dict[tuple[str, str], DailyContractAggregate], dict[str, Any]] | None:
        path = self._cache_path(trade_date)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_symbols = {normalize_symbol(item) for item in payload.get("symbols", [])}
            rows = payload.get("aggregates")
            if payload.get("schema_version") != 1 or payload.get("trade_date") != trade_date or not symbols.issubset(cached_symbols):
                return None
            if not isinstance(rows, list):
                return None
            parsed: dict[tuple[str, str], DailyContractAggregate] = {}
            for row in rows:
                if not isinstance(row, dict):
                    return None
                aggregate = DailyContractAggregate(**row)
                parsed[(aggregate.ticker, aggregate.option_chain_id)] = aggregate
            meta = dict(payload.get("source") or {})
            meta.update({"trade_date": trade_date, "cache": "hit", "aggregate_count": len(parsed)})
            return parsed, meta
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(
        self, trade_date: str, symbols: set[str], aggregates: dict[tuple[str, str], DailyContractAggregate], source: dict[str, Any]
    ) -> None:
        payload = {
            "schema_version": 1,
            "trade_date": trade_date,
            "symbols": sorted(symbols),
            "source": source,
            "aggregates": [asdict(item) for _, item in sorted(aggregates.items())],
        }
        destination = self._cache_path(trade_date)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def _download_archive(self, trade_date: str, destination: Path) -> dict[str, Any]:
        url = f"{self.base_url}/api/option-trades/full-tape/{trade_date}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/zip, application/json",
            "UW-CLIENT-API-ID": self.client_api_id,
        }
        try:
            response = self.session.get(url, headers=headers, timeout=(20, 300), stream=True)
        except requests.RequestException as exc:
            raise RunnerError(f"Unusual Whales Full Tape request failed for {trade_date}: {exc}") from exc
        if not response.ok:
            body = response.text[:500]
            if response.status_code == 403:
                raise RunnerError(
                    f"Unusual Whales Full Tape access was refused for {trade_date} (403). "
                    f"Check the account historical-lookback entitlement. {body}"
                )
            raise RunnerError(f"Unusual Whales Full Tape request failed for {trade_date} ({response.status_code}): {body}")
        content_type = response.headers.get("Content-Type", "")
        temp_path = destination.with_suffix(destination.suffix + ".part")
        byte_count = 0
        try:
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)
                        byte_count += len(chunk)
            if byte_count < 100:
                raise RunnerError(f"Unusual Whales Full Tape archive for {trade_date} was unexpectedly small")
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
            response.close()
        return {"trade_date": trade_date, "cache": "miss", "archive_bytes": byte_count, "content_type": content_type}

    @staticmethod
    def _aggregate_archive(archive_path: Path, trade_date: str, symbols: set[str]) -> tuple[dict[tuple[str, str], DailyContractAggregate], dict[str, int]]:
        """Aggregate one ZIP's non-cancelled executions; never sum UW running volume."""
        required = {
            "underlying_symbol", "executed_at", "option_chain_id", "expiry", "option_type", "strike", "size", "premium", "canceled"
        }
        output: dict[tuple[str, str], DailyContractAggregate] = {}
        source_rows = 0
        matched_rows = 0
        rejected_rows = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                csv_members = [info for info in archive.infolist() if not info.is_dir() and info.filename.lower().endswith(".csv")]
                if len(csv_members) != 1:
                    raise RunnerError(f"Full Tape archive {archive_path.name} must contain exactly one CSV")
                with archive.open(csv_members[0]) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    reader = csv.DictReader(text)
                    fields = set(reader.fieldnames or [])
                    missing = sorted(required.difference(fields))
                    if missing:
                        raise RunnerError(f"Full Tape archive is missing required columns: {', '.join(missing)}")
                    for row in reader:
                        source_rows += 1
                        ticker = normalize_symbol(row.get("underlying_symbol"))
                        if ticker not in symbols:
                            continue
                        if truthy(row.get("canceled")):
                            continue
                        option_chain_id = str(row.get("option_chain_id") or "").strip().upper()
                        option_type = normalize_option_type(row.get("option_type"))
                        strike = num(row.get("strike"))
                        size = num(row.get("size"))
                        premium = num(row.get("premium"))
                        expiry = str(row.get("expiry") or "")[:10]
                        executed_at = str(row.get("executed_at") or "")
                        if not option_chain_id or option_type is None or strike is None or size is None or premium is None or not expiry or not executed_at:
                            rejected_rows += 1
                            continue
                        if size < 0 or premium < 0:
                            rejected_rows += 1
                            continue
                        matched_rows += 1
                        key = (ticker, option_chain_id)
                        aggregate = output.get(key)
                        if aggregate is None:
                            aggregate = DailyContractAggregate(
                                ticker=ticker,
                                option_chain_id=option_chain_id,
                                expiry=expiry,
                                option_type=option_type,
                                strike=float(strike),
                            )
                            output[key] = aggregate
                        aggregate.volume += float(size)
                        aggregate.premium += float(premium)
                        aggregate.trade_count += 1
                        if executed_at >= aggregate.last_executed_at:
                            aggregate.last_executed_at = executed_at
                            aggregate.implied_volatility = num(row.get("implied_volatility"))
                            aggregate.delta = num(row.get("delta"))
                            aggregate.open_interest = num(row.get("open_interest"))
                            aggregate.underlying_price = num(row.get("underlying_price"))
        except zipfile.BadZipFile as exc:
            raise RunnerError(f"Invalid Full Tape ZIP for {trade_date}: {archive_path}") from exc
        return output, {"source_rows": source_rows, "matched_rows": matched_rows, "rejected_rows": rejected_rows}

    def aggregates_for_date(self, trade_date: str, symbols: set[str]) -> tuple[dict[tuple[str, str], DailyContractAggregate], dict[str, Any]]:
        cached = self._load_cache(trade_date, symbols)
        if cached is not None:
            return cached
        with tempfile.TemporaryDirectory(prefix="uw-flow-tape-", dir=str(self.cache_dir)) as temporary_dir:
            temporary_path = Path(temporary_dir) / f"{trade_date}.zip"
            source = self._download_archive(trade_date, temporary_path)
            aggregates, metrics = self._aggregate_archive(temporary_path, trade_date, symbols)
            source.update(metrics)
            source["aggregate_count"] = len(aggregates)
            if self.keep_archives:
                persistent = self.cache_dir / "archives" / f"{trade_date}.zip"
                shutil.copyfile(temporary_path, persistent)
                source["archive_path"] = str(persistent)
        self._write_cache(trade_date, symbols, aggregates, source)
        return aggregates, source


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def score_unusualness(event_volume: float, prior_volumes: list[float]) -> dict[str, Any]:
    average = mean(prior_volumes)
    med = float(median(prior_volumes)) if prior_volumes else 0.0
    maximum = max(prior_volumes) if prior_volumes else 0.0
    total = sum(prior_volumes)
    score = 0
    if average > 0 and event_volume > 10 * average:
        score += 3
    elif average > 0 and event_volume > 5 * average:
        score += 2
    if med == 0 and event_volume > 500:
        score += 2
    if event_volume > maximum:
        score += 2
    if event_volume > total:
        score += 1
    if maximum > 0 and event_volume <= average * 1.25:
        score -= 1
    if maximum >= event_volume * 0.75 and maximum > 500:
        score -= 2
    rating = "normal"
    if score >= 7:
        rating = "extreme"
    elif score >= 5:
        rating = "very_unusual"
    elif score >= 3:
        rating = "unusual"
    elif score >= 1:
        rating = "moderate"
    return {
        "average": average,
        "median": med,
        "maximum": maximum,
        "total": total,
        "nonzero": sum(value > 0 for value in prior_volumes),
        "score": score,
        "rating": rating,
    }


def iv_context(event_iv: float | None, prior_ivs: list[float], cfg: FlowConfig) -> dict[str, Any]:
    valid = [item for item in prior_ivs if item > 0]
    if event_iv is None or not valid:
        return {"trend": "unknown", "delta": None, "prior_average": None}
    prior_average = mean(valid)
    delta = event_iv - prior_average
    trend = "flat"
    if delta >= cfg.iv_rise_threshold:
        trend = "rising"
    elif delta <= cfg.iv_fall_threshold:
        trend = "falling"
    return {"trend": trend, "delta": rounded(delta, 4), "prior_average": rounded(prior_average, 4)}


def moneyness(side: str, strike: float, stock_price: float | None) -> str:
    if stock_price is None or stock_price <= 0 or strike <= 0:
        return "atm"
    ratio = stock_price / strike if side == "C" else strike / stock_price
    if ratio >= 1.25:
        return "deep_itm"
    if ratio >= 1.05:
        return "itm"
    if ratio >= 0.95:
        return "atm"
    if ratio >= 0.75:
        return "otm"
    return "far_otm"


def directional_contribution(
    side: str, premium: float, money: str, days: int, abs_delta: float, unusual_rating: str, context: dict[str, Any], cfg: FlowConfig
) -> dict[str, Any]:
    is_unusual = unusual_rating in {"extreme", "very_unusual", "unusual"}
    if side == "C":
        structure = "outright_call"
        if money == "deep_itm" and abs_delta >= 0.85:
            structure, direction = "stock_replacement", 0.6
        elif days <= 21 and money == "deep_itm":
            structure, direction = "roll", 0.1
        else:
            direction = 0.7
    else:
        structure = "protective_put" if money in {"otm", "far_otm"} else "outright_put"
        direction = -0.7
    iv_multiplier = 1.0
    if context["trend"] == "rising":
        iv_multiplier = cfg.iv_rise_multiplier
    elif context["trend"] == "falling":
        iv_multiplier = cfg.iv_fall_multiplier
    unusual_multiplier = 1.15 if is_unusual else 1.0
    return {
        "structure": structure,
        "directional_weight": direction,
        "iv_multiplier": iv_multiplier,
        "contribution": direction * premium * iv_multiplier * unusual_multiplier,
        "is_unusual": is_unusual,
    }


def contract_label(item: DailyContractAggregate) -> str:
    strike_text = str(int(item.strike)) if item.strike.is_integer() else str(item.strike)
    return f"{item.ticker} {item.expiry} {strike_text}{'C' if item.option_type == 'call' else 'P'}"


def candidate_rows(
    event_aggregates: dict[tuple[str, str], DailyContractAggregate], event_date: str, cfg: FlowConfig
) -> dict[str, list[DailyContractAggregate]]:
    """Apply the original event-day volume and premium gates to UW aggregates."""
    event_day = parse_iso_date(event_date)
    output: dict[str, list[DailyContractAggregate]] = defaultdict(list)
    for aggregate in event_aggregates.values():
        try:
            dte = (parse_iso_date(aggregate.expiry) - event_day).days
        except RunnerError:
            continue
        if cfg.min_dte <= dte <= cfg.max_dte and aggregate.volume >= cfg.min_volume and aggregate.premium >= cfg.min_premium:
            output[aggregate.ticker].append(aggregate)
    for ticker in output:
        output[ticker].sort(key=lambda item: (-item.premium, -item.volume, item.option_chain_id))
        del output[ticker][cfg.max_candidates_per_symbol :]
    return output


def aggregate_symbol(
    ticker: str,
    category: str | None,
    event_date: str,
    pa_trade_date: str,
    candidates: list[DailyContractAggregate],
    prior_dates: list[str],
    prior_rows_by_date: dict[str, dict[tuple[str, str], DailyContractAggregate]],
    cfg: FlowConfig,
) -> dict[str, Any]:
    """Apply the existing unusualness, IV-context, and directional-bias logic."""
    stock_price = next((item.underlying_price for item in candidates if item.underlying_price and item.underlying_price > 0), None)
    event_day = parse_iso_date(event_date)
    trades: list[dict[str, Any]] = []
    for candidate in candidates:
        prior_volumes: list[float] = []
        prior_ivs: list[float] = []
        for prior_date in prior_dates:
            row = prior_rows_by_date.get(prior_date, {}).get((ticker, candidate.option_chain_id))
            prior_volumes.append(row.volume if row is not None else 0.0)
            if row is not None and row.implied_volatility is not None:
                prior_ivs.append(row.implied_volatility)
        base = score_unusualness(candidate.volume, prior_volumes)
        context = iv_context(candidate.implied_volatility, prior_ivs, cfg)
        side = "C" if candidate.option_type == "call" else "P"
        money = moneyness(side, candidate.strike, stock_price)
        days = (parse_iso_date(candidate.expiry) - event_day).days
        direction = directional_contribution(
            side, candidate.premium, money, days, abs(candidate.delta or 0.0), str(base["rating"]), context, cfg
        )
        trades.append({
            "contract_symbol": contract_label(candidate),
            "uw_option_chain_id": candidate.option_chain_id,
            "option_type": "Call" if side == "C" else "Put",
            "expiration": candidate.expiry,
            "strike": candidate.strike,
            "uw_event_volume": rounded(candidate.volume, 2),
            "estimated_premium": rounded(candidate.premium, 2),
            "avg_volume_3d": rounded(float(base["average"]), 2),
            "median_volume_3d": rounded(float(base["median"]), 2),
            "max_volume_3d": rounded(float(base["maximum"]), 2),
            "prior_volumes": [rounded(value, 2) for value in prior_volumes],
            "unusualness_score": base["score"],
            "unusualness_rating": base["rating"],
            "possible_structure": direction["structure"],
            "iv": candidate.implied_volatility,
            "iv_prior_avg": context["prior_average"],
            "iv_delta": context["delta"],
            "iv_trend": context["trend"],
            "directional_weight": rounded(float(direction["directional_weight"]), 3),
            "iv_multiplier": direction["iv_multiplier"],
            "directional_premium": rounded(float(direction["contribution"]), 2),
            "moneyness": money,
            "dte": days,
            "delta": candidate.delta,
            "open_interest": candidate.open_interest,
            "is_unusual": direction["is_unusual"],
            "source_trade_count": candidate.trade_count,
            "last_execution": candidate.last_executed_at,
        })

    unusual = [trade for trade in trades if trade["is_unusual"]]
    total_call_premium = sum(float(trade["estimated_premium"] or 0) for trade in trades if trade["option_type"] == "Call")
    total_put_premium = sum(float(trade["estimated_premium"] or 0) for trade in trades if trade["option_type"] == "Put")
    max_score = max((int(trade["unusualness_score"]) for trade in trades), default=0)
    net_directional = sum(float(trade["directional_premium"] or 0) for trade in trades)
    gross_directional = sum(abs(float(trade["directional_premium"] or 0)) for trade in trades)
    tilt_share = net_directional / gross_directional if gross_directional else 0.0
    unusual_premium = sum(float(trade["estimated_premium"] or 0) for trade in unusual)
    passes_gate = len(unusual) >= cfg.min_unusual and unusual_premium >= cfg.min_unusual_premium

    if not passes_gate:
        bias, strength = "insufficient_evidence", "none"
    elif tilt_share >= 0.20:
        bias = "bullish"
        strength = "strong" if abs(tilt_share) >= cfg.bias_net_strong else "moderate"
    elif tilt_share <= -0.20:
        bias = "bearish_or_defensive"
        strength = "strong" if abs(tilt_share) >= cfg.bias_net_strong else "moderate"
    else:
        bias, strength = "mixed", "weak"

    if bias == "bullish":
        pool, bias_label = [trade for trade in trades if trade["option_type"] == "Call"], "bullish/long exposure"
    elif bias == "bearish_or_defensive":
        pool, bias_label = [trade for trade in trades if trade["option_type"] == "Put"], "bearish/defensive (put-weighted)"
    elif bias == "mixed":
        pool, bias_label = trades, "mixed"
    else:
        pool, bias_label = trades, "insufficient evidence"
    by_structure: dict[str, float] = {}
    for trade in pool or trades:
        key = str(trade["possible_structure"])
        by_structure[key] = by_structure.get(key, 0.0) + abs(float(trade["estimated_premium"] or 0))
    dominant_structure = max(by_structure, key=by_structure.get) if by_structure else "none"
    bullish = sum(float(trade["directional_premium"] or 0) for trade in trades if float(trade["directional_premium"] or 0) > 0)
    bearish = abs(sum(float(trade["directional_premium"] or 0) for trade in trades if float(trade["directional_premium"] or 0) < 0))
    rising_count = sum(trade["iv_trend"] == "rising" for trade in unusual)
    sorted_trades = sorted(trades, key=lambda item: abs(float(item["estimated_premium"] or 0)), reverse=True)
    summary = (
        f"{len(unusual)}/{len(trades)} trades unusual+ (${unusual_premium / 1_000_000:.1f}M unusual premium). "
        f"{strength} {bias_label}; {rising_count} unusual w/ rising IV. Dominant: {dominant_structure}."
        if passes_gate
        else f"Insufficient evidence ({len(unusual)} unusual, ${unusual_premium / 1_000_000:.1f}M premium; "
        f"gate {cfg.min_unusual}+ trade & ${cfg.min_unusual_premium / 1_000_000:.0f}M). No directional call."
    )
    return {
        "ticker": ticker,
        "category": category,
        "signal_date": event_date,
        "pa_trade_date": pa_trade_date,
        "current_price": stock_price,
        "total_call_premium": rounded(total_call_premium, 2),
        "total_put_premium": rounded(total_put_premium, 2),
        "call_put_premium_ratio": rounded(total_call_premium / total_put_premium, 2) if total_put_premium else None,
        "unusual_call_count": sum(trade["option_type"] == "Call" for trade in unusual),
        "unusual_put_count": sum(trade["option_type"] == "Put" for trade in unusual),
        "extreme_trade_count": sum(trade["unusualness_rating"] == "extreme" for trade in trades),
        "max_unusualness_score": max_score,
        "avg_unusualness_score": rounded(mean([float(trade["unusualness_score"]) for trade in trades]), 2) if trades else 0.0,
        "bullish_flow_score": rounded(bullish, 2),
        "bearish_flow_score": rounded(bearish, 2),
        "hedging_flow_score": rounded(bearish, 2),
        "unusual_premium": rounded(unusual_premium, 2),
        "net_directional_premium": rounded(net_directional, 2),
        "directional_confidence": rounded(min(1.0, abs(tilt_share)), 2) if passes_gate else 0.0,
        "net_option_flow_bias": bias,
        "bias_strength": strength,
        "dominant_structure": dominant_structure,
        "iv_rising_unusual_count": rising_count,
        "top_contracts": [trade["contract_symbol"] for trade in sorted_trades[:5]],
        "option_flow_summary": summary,
        "jlaw_source_row": None,
        "trade_details": sorted_trades[: cfg.top_trades],
    }


def no_signal_result(ticker: str, category: str | None, event_date: str, pa_trade_date: str, reason: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "category": category,
        "signal_date": event_date,
        "pa_trade_date": pa_trade_date,
        "current_price": None,
        "total_call_premium": 0.0,
        "total_put_premium": 0.0,
        "call_put_premium_ratio": None,
        "unusual_call_count": 0,
        "unusual_put_count": 0,
        "extreme_trade_count": 0,
        "max_unusualness_score": 0,
        "avg_unusualness_score": 0.0,
        "bullish_flow_score": 0.0,
        "bearish_flow_score": 0.0,
        "hedging_flow_score": 0.0,
        "unusual_premium": 0.0,
        "net_directional_premium": 0.0,
        "directional_confidence": 0.0,
        "net_option_flow_bias": "no_data",
        "bias_strength": "none",
        "dominant_structure": "none",
        "iv_rising_unusual_count": 0,
        "top_contracts": [],
        "option_flow_summary": f"No qualifying unusual option flow detected. {reason}",
        "jlaw_source_row": None,
        "trade_details": [],
        "_no_signal": True,
    }


def signal_payload(result: dict[str, Any]) -> dict[str, Any]:
    columns = (
        "ticker", "signal_date", "current_price", "total_call_premium", "total_put_premium",
        "call_put_premium_ratio", "unusual_call_count", "unusual_put_count", "extreme_trade_count",
        "max_unusualness_score", "avg_unusualness_score", "bullish_flow_score", "bearish_flow_score",
        "hedging_flow_score", "net_option_flow_bias", "dominant_structure", "top_contracts",
        "option_flow_summary", "jlaw_source_row", "unusual_premium", "net_directional_premium",
        "directional_confidence", "bias_strength", "iv_rising_unusual_count",
    )
    return {column: result.get(column) for column in columns}


def write_results_supabase(
    session: requests.Session, base_url: str, key: str, signal_table: str, position_actions_table: str, rows: list[dict[str, Any]]
) -> dict[str, int]:
    """Append signals and backfill matching position-action summaries."""
    written, position_updates, position_failures = 0, 0, 0
    for row in rows:
        response = session.post(
            supabase_table_url(base_url, signal_table),
            headers=supabase_headers(key, prefer="return=minimal"),
            json=signal_payload(row),
            timeout=45,
        )
        if not response.ok:
            raise RunnerError(f"Supabase signal insert failed ({response.status_code}): {response.text[:500]}")
        written += 1
        update = session.patch(
            supabase_table_url(base_url, position_actions_table),
            headers=supabase_headers(key, prefer="return=minimal"),
            params={"symbol": f"eq.{row['ticker']}", "trade_date": f"eq.{row['pa_trade_date']}"},
            json={"option_flow_summary": row["option_flow_summary"]},
            timeout=45,
        )
        if update.ok:
            position_updates += 1
        else:
            position_failures += 1
            logging.warning("Position-actions update failed for %s (%s): %s", row["ticker"], update.status_code, update.text[:300])
    return {"written": written, "position_action_updates": position_updates, "position_action_update_failures": position_failures}


def run_flow(
    cfg: FlowConfig,
    raw_watchlist: list[dict[str, Any]],
    categories: tuple[str, ...],
    session: requests.Session,
    tape_client: UnusualWhalesFullTapeClient,
    event_date: str,
    prior_dates: list[str],
    emit_no_signal: bool,
    write_supabase: bool,
    supabase_base: str,
    supabase_key: str,
    signal_table: str,
    position_actions_table: str,
) -> FlowReport:
    report = FlowReport(
        run_id=uuid.uuid4().hex,
        model_version=MODEL_VERSION,
        started_at=datetime.now(timezone.utc).isoformat(),
        event_date=event_date,
        prior_dates=prior_dates,
        supabase={"requested": write_supabase, "written": 0, "position_action_updates": 0, "position_action_update_failures": 0},
    )
    symbols = prepare_watchlist(raw_watchlist, categories, cfg.max_symbols, report)
    report.input_symbols = len(raw_watchlist)
    report.accepted_us_symbols = len(symbols)
    if not symbols:
        raise RunnerError("No explicitly US-listed symbols remained after watchlist filtering")
    wanted = {item["ticker"] for item in symbols}
    day_aggregates: dict[str, dict[tuple[str, str], DailyContractAggregate]] = {}
    for trade_date in [event_date, *prior_dates]:
        aggregates, source = tape_client.aggregates_for_date(trade_date, wanted)
        day_aggregates[trade_date] = aggregates
        report.tape_sources.append(source)
        logging.info(
            "Full Tape %s cache=%s aggregate_contracts=%s matched_rows=%s",
            trade_date, source.get("cache"), source.get("aggregate_count"), source.get("matched_rows", "cached"),
        )
    candidates_by_symbol = candidate_rows(day_aggregates[event_date], event_date, cfg)
    pa_trade_date = datetime.now(timezone.utc).date().isoformat()
    for watch_item in symbols:
        ticker, category = watch_item["ticker"], watch_item["category"]
        candidates = candidates_by_symbol.get(ticker, [])
        if not candidates:
            reason = "No event-day contracts passed Full Tape volume and premium thresholds."
            report.skipped_symbols.append({"symbol": ticker, "reason": reason})
            if emit_no_signal:
                report.results.append(no_signal_result(ticker, category, event_date, pa_trade_date, reason))
            continue
        result = aggregate_symbol(ticker, category, event_date, pa_trade_date, candidates, prior_dates, day_aggregates, cfg)
        report.results.append(result)
        report.processed_symbols += 1
        report.signal_count += 1
        logging.info("%s: %s", ticker, result["option_flow_summary"])
    if write_supabase:
        if not (supabase_base and supabase_key):
            raise RunnerError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) are required with --write-supabase")
        report.supabase.update(write_results_supabase(
            session, supabase_base, supabase_key, signal_table, position_actions_table, report.results
        ))
    return report


def output_report(report: FlowReport, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def summary(report: FlowReport, output: Path) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "provider": "unusual-whales-full-tape",
        "event_date": report.event_date,
        "prior_dates": report.prior_dates,
        "input_symbols": report.input_symbols,
        "accepted_us_symbols": report.accepted_us_symbols,
        "processed_symbols": report.processed_symbols,
        "signals": report.signal_count,
        "skipped": len(report.skipped_symbols),
        "failed": len(report.failed_symbols),
        "supabase_written": report.supabase.get("written", 0),
        "output": str(output),
    }


def make_config(args: argparse.Namespace) -> FlowConfig:
    cfg = FlowConfig(
        lookback_days=args.lookback_days,
        max_symbols=args.max_symbols,
        max_candidates_per_symbol=args.max_candidates_per_symbol,
        min_volume=args.min_volume,
        min_premium=args.min_premium,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
    )
    if cfg.lookback_days < 1 or cfg.max_symbols < 1 or cfg.max_candidates_per_symbol < 1:
        raise RunnerError("lookback-days, max-symbols, and max-candidates-per-symbol must be positive")
    if cfg.min_volume < 0 or cfg.min_premium < 0 or cfg.min_dte < 0 or cfg.max_dte < cfg.min_dte:
        raise RunnerError("Invalid screening thresholds")
    return cfg


def resolve_watchlist(
    args: argparse.Namespace, session: requests.Session, categories: tuple[str, ...], supabase_base: str, supabase_key: str
) -> list[dict[str, Any]]:
    if args.symbols:
        return [{"symbol": item.strip(), "category": categories[0], "exchange": "US"} for item in args.symbols.split(",") if item.strip()]
    if args.watchlist:
        return load_watchlist_file(args.watchlist)
    if not (supabase_base and supabase_key):
        raise RunnerError("Provide --symbols or --watchlist, or configure SUPABASE_URL and SUPABASE_KEY")
    return load_watchlist_supabase(session, supabase_base, supabase_key, args.watchlist_table, categories)


def execute(args: argparse.Namespace) -> tuple[FlowReport, Path]:
    load_dotenv(args.dotenv)
    cfg = make_config(args)
    categories = tuple(item.strip() for item in args.categories.split(",") if item.strip())
    if not categories:
        raise RunnerError("At least one watchlist category is required")
    session = build_session()
    supabase_base = optional_env("SUPABASE_URL")
    supabase_key = optional_env("SUPABASE_KEY") or optional_env("SUPABASE_SERVICE_ROLE_KEY")
    raw_watchlist = resolve_watchlist(args, session, categories, supabase_base, supabase_key)
    event_date, prior_dates = dates_for_run(args.event_date, cfg.lookback_days)
    tape_client = UnusualWhalesFullTapeClient(
        session=session,
        api_key=optional_env("UW_API_KEY") or optional_env("UNUSUAL_WHALES_API_KEY"),
        cache_dir=args.cache_dir,
        base_url=args.uw_base_url,
        client_api_id=args.uw_client_api_id,
        keep_archives=args.keep_full_tape_archives,
    )
    report = run_flow(
        cfg=cfg,
        raw_watchlist=raw_watchlist,
        categories=categories,
        session=session,
        tape_client=tape_client,
        event_date=event_date,
        prior_dates=prior_dates,
        emit_no_signal=args.emit_no_signal,
        write_supabase=args.write_supabase,
        supabase_base=supabase_base,
        supabase_key=supabase_key,
        signal_table=args.signal_table,
        position_actions_table=args.position_actions_table,
    )
    output = args.output
    if output.is_dir() or str(output).endswith("/"):
        output = output / f"uw_unusual_option_flow_{report.run_id}.json"
    output_report(report, output)
    return report, output


def create_app(args: argparse.Namespace) -> Any:
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RunnerError("Install fastapi and uvicorn to use --serve") from exc
    app = FastAPI(title="Unusual Whales Full Tape Unusual Option Flow", version=MODEL_VERSION)

    def webhook_run() -> dict[str, Any]:
        try:
            report, output = execute(args)
            return {"status": "completed", **summary(report, output)}
        except RunnerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover
            logging.exception("Webhook flow failed")
            raise HTTPException(status_code=500, detail="Workflow execution failed; inspect server logs.") from exc

    app.post("/task3-uw-unusual-flow-jlaw")(webhook_run)
    app.post("/task3-orats-unusual-flow-jlaw", include_in_schema=False)(webhook_run)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "model_version": MODEL_VERSION}

    return app


def self_test() -> dict[str, Any]:
    """Run deterministic parsing, calendar, US-universe, and scoring checks without network access."""
    checks = 0
    with tempfile.TemporaryDirectory(prefix="uw-flow-selftest-") as directory:
        archive_path = Path(directory) / "fixture.zip"
        fields = [
            "underlying_symbol", "executed_at", "option_chain_id", "expiry", "option_type", "strike", "size", "premium", "canceled",
            "implied_volatility", "delta", "open_interest", "underlying_price",
        ]
        rows = [
            ["AAPL", "2026-10-12T14:00:00.000Z", "AAPL261120C00110000", "2026-11-20", "call", "110", "600", "1200000", "false", "0.30", "0.50", "900", "100"],
            ["AAPL", "2026-10-12T15:00:00.000Z", "AAPL261120C00110000", "2026-11-20", "call", "110", "600", "1200000", "false", "0.35", "0.55", "1000", "101"],
            ["AAPL", "2026-10-12T15:01:00.000Z", "AAPL261120C00110000", "2026-11-20", "call", "110", "999", "9999999", "true", "0.99", "0.99", "9999", "101"],
            ["TSLA", "2026-10-12T15:02:00.000Z", "TSLA261120P00250000", "2026-11-20", "put", "250", "3000", "3000000", "false", "0.40", "-0.40", "100", "240"],
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("full_tape.csv", buffer.getvalue())
        aggregates, metrics = UnusualWhalesFullTapeClient._aggregate_archive(archive_path, "2026-10-12", {"AAPL"})
        candidate = aggregates[("AAPL", "AAPL261120C00110000")]
        assert len(aggregates) == 1 and metrics["matched_rows"] == 2
        checks += 1
        assert candidate.volume == 1200 and candidate.premium == 2_400_000 and candidate.trade_count == 2
        checks += 1
        assert candidate.implied_volatility == 0.35 and candidate.delta == 0.55 and candidate.open_interest == 1000 and candidate.underlying_price == 101
        checks += 1
        assert is_trading_day(date(2026, 4, 3)) is False and previous_trading_day(date(2026, 4, 6)) == date(2026, 4, 2)
        checks += 1
        assert is_us_listing({"exchange": "NASDAQ"}) and not is_us_listing({"exchange": "LSE"}) and not is_us_listing({"symbol": "AAPL"})
        checks += 1
        assert prepare_watchlist([{"symbol": "AAPL", "category": "core", "exchange": "NYSE"}, {"symbol": "LSE1", "category": "core", "exchange": "LSE"}], ("core",), 5, FlowReport("x", "x", "x", "x", [])) == [{"ticker": "AAPL", "category": "core"}]
        checks += 1
        prior_rows = {
            "2026-10-09": {("AAPL", candidate.option_chain_id): DailyContractAggregate("AAPL", candidate.option_chain_id, candidate.expiry, "call", 110, volume=200, implied_volatility=0.20)},
            "2026-10-08": {},
            "2026-10-07": {("AAPL", candidate.option_chain_id): DailyContractAggregate("AAPL", candidate.option_chain_id, candidate.expiry, "call", 110, volume=100, implied_volatility=0.22)},
        }
        cfg = FlowConfig()
        candidates = candidate_rows(aggregates, "2026-10-12", cfg)["AAPL"]
        assert len(candidates) == 1 and candidates[0].option_chain_id == candidate.option_chain_id
        checks += 1
        result = aggregate_symbol("AAPL", "core", "2026-10-12", "2026-10-13", candidates, ["2026-10-09", "2026-10-08", "2026-10-07"], prior_rows, cfg)
        assert result["net_option_flow_bias"] == "bullish" and result["unusual_call_count"] == 1 and result["unusual_premium"] == 2_400_000
        checks += 1
        assert result["trade_details"][0]["iv_trend"] == "rising" and result["trade_details"][0]["prior_volumes"] == [200, 0, 100]
        checks += 1
        payload = signal_payload(result)
        assert set(payload) == {
            "ticker", "signal_date", "current_price", "total_call_premium", "total_put_premium", "call_put_premium_ratio", "unusual_call_count", "unusual_put_count", "extreme_trade_count", "max_unusualness_score", "avg_unusualness_score", "bullish_flow_score", "bearish_flow_score", "hedging_flow_score", "net_option_flow_bias", "dominant_structure", "top_contracts", "option_flow_summary", "jlaw_source_row", "unusual_premium", "net_directional_premium", "directional_confidence", "bias_strength", "iv_rising_unusual_count"
        }
        checks += 1
    return {"ok": True, "self_test": "passed", "checks": checks, "model_version": MODEL_VERSION}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run unusual option flow from Unusual Whales Full Tape archives.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--symbols", help="Comma-separated US ticker symbols for an ad hoc run.")
    source.add_argument("--watchlist", type=Path, help="CSV/JSON watchlist with symbol/ticker, category, and explicit US exchange fields.")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"), help="Optional dotenv file; process environment wins.")
    parser.add_argument("--output", type=Path, default=Path("uw_unusual_option_flow_run.json"), help="JSON audit output file or directory.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Local cache for reduced contract-day aggregates.")
    parser.add_argument("--event-date", help="Completed NYSE event date (YYYY-MM-DD); default is the latest completed session.")
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES), help="Comma-separated watchlist categories in priority order.")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--max-symbols", type=int, default=200)
    parser.add_argument("--max-candidates-per-symbol", type=int, default=25)
    parser.add_argument("--min-volume", type=int, default=500)
    parser.add_argument("--min-premium", type=float, default=1_000_000.0)
    parser.add_argument("--min-dte", type=int, default=0)
    parser.add_argument("--max-dte", type=int, default=180)
    parser.add_argument("--uw-base-url", default=optional_env("UW_BASE", UW_BASE_URL))
    parser.add_argument("--uw-client-api-id", default=optional_env("UW_CLIENT_API_ID", "100001"))
    parser.add_argument("--keep-full-tape-archives", action="store_true", help="Keep downloaded source ZIPs under cache-dir/archives; default retains only reduced aggregates.")
    parser.add_argument("--watchlist-table", default=optional_env("SUPABASE_WATCHLIST_TABLE", DEFAULT_WATCHLIST_TABLE))
    parser.add_argument("--signal-table", default=optional_env("SUPABASE_SIGNAL_TABLE", DEFAULT_SIGNAL_TABLE))
    parser.add_argument("--position-actions-table", default=optional_env("SUPABASE_POSITION_ACTIONS_TABLE", DEFAULT_POSITION_ACTIONS_TABLE))
    parser.add_argument("--emit-no-signal", action="store_true", help="Emit no-data rows for symbols without qualifying event-day flow.")
    parser.add_argument("--write-supabase", action="store_true", help="Explicitly append signals and update position-action summaries.")
    parser.add_argument("--serve", action="store_true", help="Serve POST webhook paths and GET /healthz.")
    parser.add_argument("--host", default="127.0.0.1", help="Webhook bind address when --serve is used.")
    parser.add_argument("--port", type=int, default=8000, help="Webhook port when --serve is used.")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--self-test", action="store_true", help="Run deterministic offline checks and exit without network or writes.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False))
        return 0
    if args.serve:
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover
            raise RunnerError("Install uvicorn to use --serve") from exc
        uvicorn.run(create_app(args), host=args.host, port=args.port, log_level=args.log_level.lower())
        return 0
    report, destination = execute(args)
    print(json.dumps(summary(report, destination), ensure_ascii=False))
    return 2 if report.failed_symbols else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s", stream=os.sys.stderr)
        logging.error("%s", exc)
        raise SystemExit(1)
