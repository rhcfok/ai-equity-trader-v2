#!/usr/bin/env python3
"""Run the dark-pool accumulation workflow from Unusual Whales dark-pool prints.

The runner downloads the most recent dark-pool prints for each requested
US-listed watchlist symbol, reduces them into a daily accumulation summary
(buy/sell pressure versus NBBO, block prints, top demand level), produces a
JSON audit artifact, and optionally writes to Supabase:

- raw prints  -> ``darkpool_prints`` (insert with duplicate-ignore on
  ``(symbol, executed_at, tracking_id)``)
- aggregates  -> ``dp_*`` columns on ``option-opportunity-davis`` rows for the
  same symbol and report date (the Davis CSP/BPS opportunity layer)

No orders are placed and no brokerage is accessed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MODEL_VERSION = "Unusual Whales Dark Pool Accumulation v1.0"
UW_BASE_URL = "https://api.unusualwhales.com"
DEFAULT_RAW_TABLE = "darkpool_prints"
DEFAULT_OPPORTUNITY_TABLE = "option-opportunity-davis"
DEFAULT_WATCHLIST_TABLE = "watchlist"
DEFAULT_CATEGORIES = ("core", "satellite", "watch1")
DEFAULT_CACHE_DIR = Path("uw_unusual_option_flow_cache")
DEFAULT_BLOCK_SIZE = 10_000
try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # Windows Python without the optional tzdata package
    EASTERN = timezone(timedelta(hours=-5))


class RunnerError(RuntimeError):
    """Raised for invalid configuration, source data, or persistence failures."""


@dataclass
class DarkPoolConfig:
    block_size: int = DEFAULT_BLOCK_SIZE
    include_derivative: bool = False


@dataclass
class DarkPoolReport:
    run_id: str
    model_version: str
    started_at: str
    opportunity_date: str
    input_symbols: int = 0
    processed_symbols: int = 0
    skipped_symbols: list[dict[str, str]] = field(default_factory=list)
    failed_symbols: list[dict[str, str]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
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
    return os.environ.get(name, default).strip()


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=2.0, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST", "PATCH"])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def supabase_headers(key: str, prefer: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Prefer": prefer,
            "Content-Type": "application/json"}


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def reduce_prints(prints: list[dict[str, Any]], symbol: str, cfg: DarkPoolConfig) -> dict[str, Any]:
    """Reduce raw dark-pool prints into one per-fetch accumulation summary.

    Classification is inferred from execution versus the print-time NBBO
    midpoint (>= mid counts as buy-side pressure). Cancelled prints are
    dropped. ``derivative_priced`` prints are excluded from the aggregate
    unless ``include_derivative`` is set, because they represent hedging or
    rebalancing rather than directional intent.
    """
    usable: list[dict[str, Any]] = []
    for item in prints:
        if item.get("canceled"):
            continue
        if not item.get("premium"):
            continue
        if not cfg.include_derivative and item.get("trade_code") == "derivative_priced":
            continue
        usable.append(item)

    total = buy = sell = blocks = block_premium = 0.0
    level_premium: dict[str, float] = {}
    timestamps: list[str] = []
    last_price: float | None = None

    for item in usable:
        premium = _float(item.get("premium")) or 0.0
        size = _int(item.get("size")) or 0
        price = _float(item.get("price"))
        bid = _float(item.get("nbbo_bid"))
        ask = _float(item.get("nbbo_ask"))
        total += premium
        mid = (ask + bid) / 2.0 if ask is not None and bid is not None else price
        if price is not None and mid is not None and price >= mid:
            buy += premium
        else:
            sell += premium
        if size >= cfg.block_size:
            blocks += 1
            block_premium += premium
        if price is not None:
            level_premium[str(price)] = level_premium.get(str(price), 0.0) + premium
        executed = item.get("executed_at")
        if executed:
            timestamps.append(str(executed))
        last_price = price if price is not None else last_price

    above_close = below_close = 0.0
    if last_price is not None:
        for item in usable:
            price = _float(item.get("price"))
            premium = _float(item.get("premium")) or 0.0
            if price is None:
                continue
            if price > last_price:
                above_close += premium
            elif price < last_price:
                below_close += premium

    top_level, top_level_premium = None, 0.0
    if level_premium:
        top_level = max(level_premium, key=lambda key: level_premium[key])
        top_level_premium = level_premium[top_level]
        top_level = float(top_level)

    net = buy - sell
    return {
        "symbol": symbol,
        "print_count": len(usable),
        "window_start": min(timestamps) if timestamps else None,
        "window_end": max(timestamps) if timestamps else None,
        "last_print_price": last_price,
        "total_premium": round(total, 2),
        "buy_premium": round(buy, 2),
        "sell_premium": round(sell, 2),
        "net_premium": round(net, 2),
        "block_count": blocks,
        "block_premium": round(block_premium, 2),
        "top_level": top_level,
        "top_level_premium": round(top_level_premium, 2),
        "above_close_premium": round(above_close, 2),
        "below_close_premium": round(below_close, 2),
        "accumulating": bool(net > 0),
    }


def opportunity_payload(agg: dict[str, Any], as_of: str) -> dict[str, Any]:
    """dp_* columns matching the migration on ``option-opportunity-davis``."""
    return {
        "dp_as_of": as_of,
        "dp_total_premium_mm": round(agg["total_premium"] / 1_000_000.0, 4),
        "dp_buy_mm": round(agg["buy_premium"] / 1_000_000.0, 4),
        "dp_sell_mm": round(agg["sell_premium"] / 1_000_000.0, 4),
        "dp_net_mm": round(agg["net_premium"] / 1_000_000.0, 4),
        "dp_blocks": agg["block_count"],
        "dp_top_level": agg["top_level"],
        "dp_top_level_mm": round(agg["top_level_premium"] / 1_000_000.0, 4),
        "dp_accumulating": agg["accumulating"],
    }


def print_row(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map one raw UW print to the ``darkpool_prints`` schema (None = unusable)."""
    tracking = _int(item.get("tracking_id"))
    if tracking is None or not item.get("executed_at"):
        return None
    codes = item.get("sale_cond_codes")
    return {
        "symbol": item.get("ticker"),
        "executed_at": item["executed_at"],
        "tracking_id": tracking,
        "size": _int(item.get("size")),
        "price": _float(item.get("price")),
        "premium": _float(item.get("premium")),
        "nbbo_bid": _float(item.get("nbbo_bid")),
        "nbbo_ask": _float(item.get("nbbo_ask")),
        "market_center": item.get("market_center"),
        "sale_cond_codes": codes if isinstance(codes, str) else json.dumps(codes) if codes else None,
        "trade_code": item.get("trade_code"),
    }


def fetch_darkpool(session: requests.Session, symbol: str, api_key: str,
                   base_url: str, client_api_id: str, timeout: int) -> list[dict[str, Any]]:
    url = f"{base_url}/api/darkpool/{symbol}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}",
               "UW-CLIENT-API-ID": client_api_id, "User-Agent": "DarkpoolWorkflow/1.0"}
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise RunnerError(f"Unexpected dark pool payload for {symbol}: {str(payload)[:200]}")
    return data


def resolve_symbols(args: argparse.Namespace, session: requests.Session,
                    supabase_base: str, supabase_key: str) -> list[dict[str, str]]:
    if args.symbols:
        return [{"symbol": item.strip(), "category": "explicit"} for item in args.symbols.split(",") if item.strip()]
    if not (supabase_base and supabase_key):
        raise RunnerError("Provide --symbols or configure SUPABASE_URL and SUPABASE_KEY")
    categories = ",".join(args.categories)
    url = f"{supabase_base}/rest/v1/{args.watchlist_table}"
    response = session.get(
        url,
        headers=supabase_headers(supabase_key, "count=exact"),
        params={"select": "symbol,category,exchange", "category": f"in.({categories})", "limit": args.max_symbols},
        timeout=60,
    )
    response.raise_for_status()
    us_exchanges = {
        "NASDAQ", "NASDAQGS", "NASDAQCM", "NASDAQGM", "NYSE", "NYSEARCA",
        "NYSE AMERICAN", "NYSE MKT", "AMEX", "ARCA", "BATS", "CBOE", "IEX", "US", "USA",
    }
    rows = response.json()
    return [{"symbol": row["symbol"], "category": row.get("category", "")}
            for row in rows if row.get("exchange") in us_exchanges]


def cache_path(args: argparse.Namespace, symbol: str) -> Path:
    return Path(args.cache_dir) / "darkpool" / f"{symbol}_{args.opportunity_date}.json"


def load_cached_prints(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
    except (OSError, ValueError):
        return None
    return None


def write_raw_prints(session: requests.Session, supabase_base: str, supabase_key: str,
                     rows: list[dict[str, Any]], raw_table: str) -> int:
    url = f"{supabase_base}/rest/v1/{raw_table}"
    written = 0
    for start in range(0, len(rows), 500):
        chunk = rows[start:start + 500]
        response = session.post(
            url,
            headers=supabase_headers(supabase_key, "resolution=ignore-duplicates, return=minimal"),
            json=chunk,
            timeout=120,
        )
        if not response.ok:
            raise RunnerError(f"darkpool_prints insert failed ({response.status_code}): {response.text[:400]}")
        written += len(chunk)
    return written


def patch_opportunity(session: requests.Session, supabase_base: str, supabase_key: str,
                      symbol: str, opportunity_date: str, payload: dict[str, Any],
                      opportunity_table: str) -> bool:
    url = f"{supabase_base}/rest/v1/{opportunity_table}"
    response = session.patch(
        url,
        headers=supabase_headers(supabase_key, "return=minimal"),
        params={"symbol": f"eq.{symbol}", "report_date": f"eq.{opportunity_date}"},
        json=payload,
        timeout=60,
    )
    if response.ok:
        return True
    if response.status_code in (400, 404):
        logging.warning("opportunity patch skipped for %s (%s): %s", symbol, response.status_code, response.text[:300])
        return False
    raise RunnerError(f"opportunity patch failed for {symbol} ({response.status_code}): {response.text[:400]}")


def output_report(report: DarkPoolReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")


def execute(args: argparse.Namespace) -> tuple[DarkPoolReport, Path]:
    load_dotenv(args.dotenv)
    configure_logging(args.log_level)
    session = build_session()
    supabase_base = optional_env("SUPABASE_URL").rstrip("/")
    supabase_key = optional_env("SUPABASE_SERVICE_ROLE_KEY") or optional_env("SUPABASE_KEY")
    api_key = optional_env("UW_API_KEY") or optional_env("UNUSUAL_WHALES_API_KEY")
    if not api_key:
        raise RunnerError("UW_API_KEY or UNUSUAL_WHALES_API_KEY not set")

    watchlist = resolve_symbols(args, session, supabase_base, supabase_key)
    cfg = DarkPoolConfig(block_size=args.block_size, include_derivative=args.include_derivative)
    started = datetime.now(timezone.utc).isoformat()
    report = DarkPoolReport(
        run_id=f"dp-{uuid.uuid4().hex[:12]}",
        model_version=MODEL_VERSION,
        started_at=started,
        opportunity_date=args.opportunity_date,
        input_symbols=len(watchlist),
    )

    for entry in watchlist:
        symbol = entry["symbol"]
        try:
            path = cache_path(args, symbol)
            prints = load_cached_prints(path)
            if prints is None:
                prints = fetch_darkpool(session, symbol, api_key, args.uw_base_url, args.uw_client_api_id, args.timeout)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(prints, ensure_ascii=False), encoding="utf-8")
                logging.info("%s: fetched %d prints", symbol, len(prints))
            else:
                logging.info("%s: %d cached prints", symbol, len(prints))
            if not prints:
                report.skipped_symbols.append({"symbol": symbol, "reason": "no prints"})
                continue
            agg = reduce_prints(prints, symbol, cfg)
            agg["category"] = entry.get("category", "")
            agg["supabase"] = {}
            if args.write_supabase:
                if not (supabase_base and supabase_key):
                    raise RunnerError("--write-supabase requires SUPABASE_URL and SUPABASE_KEY")
                raw_rows = [row for row in (print_row(item) for item in prints) if row is not None]
                if raw_rows:
                    agg["supabase"]["prints_written"] = write_raw_prints(
                        session, supabase_base, supabase_key, raw_rows, args.raw_table)
                patched = patch_opportunity(
                    session, supabase_base, supabase_key, symbol, args.opportunity_date,
                    opportunity_payload(agg, started), args.opportunity_table)
                agg["supabase"]["opportunity_patched"] = patched
            report.symbols.append(agg)
            report.processed_symbols += 1
        except Exception as exc:  # per-symbol isolation: one failure does not stop the run
            logging.exception("dark pool run failed for %s", symbol)
            report.failed_symbols.append({"symbol": symbol, "error": str(exc)[:400]})

    output = args.output
    if output.is_dir() or str(output).endswith("/"):
        output = output / f"uw_darkpool_{report.run_id}.json"
    output_report(report, output)
    return report, output


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unusual Whales dark-pool accumulation workflow")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"), help="private env file")
    parser.add_argument("--symbols", help="comma-separated tickers; overrides watchlist")
    parser.add_argument("--watchlist-table", default=optional_env("SUPABASE_WATCHLIST_TABLE", DEFAULT_WATCHLIST_TABLE))
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    parser.add_argument("--max-symbols", type=int, default=200)
    parser.add_argument("--opportunity-date", default=datetime.now(EASTERN).date().isoformat(),
                        help="report_date on option-opportunity-davis rows to patch (YYYY-MM-DD)")
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument("--include-derivative", action="store_true",
                        help="include derivative_priced prints in the aggregate")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", "--out", dest="output", type=Path, default=Path("out"),
                        help="output directory or JSON file")
    parser.add_argument("--write-supabase", action="store_true")
    parser.add_argument("--raw-table", default=optional_env("SUPABASE_DARKPOOL_TABLE", DEFAULT_RAW_TABLE))
    parser.add_argument("--opportunity-table", default=optional_env("SUPABASE_OPPORTUNITY_TABLE", DEFAULT_OPPORTUNITY_TABLE))
    parser.add_argument("--uw-base-url", default=optional_env("UW_BASE", UW_BASE_URL))
    parser.add_argument("--uw-client-api-id", default=optional_env("UW_CLIENT_API_ID", "100001"))
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--self-test", action="store_true", help="run offline deterministic checks")
    return parser


def self_test() -> dict[str, Any]:
    """Deterministic offline checks for reduction and payload builders."""
    checks = 0
    cfg = DarkPoolConfig()
    prints = [
        {"tracking_id": 1, "executed_at": "2026-10-16T14:00:00Z", "ticker": "XYZ", "size": 100, "price": 10.00,
         "premium": 1000.0, "nbbo_bid": 9.99, "nbbo_ask": 10.01, "canceled": False, "trade_code": None, "market_center": "L"},
        {"tracking_id": 2, "executed_at": "2026-10-16T14:01:00Z", "ticker": "XYZ", "size": 200, "price": 9.99,
         "premium": 1998.0, "nbbo_bid": 9.99, "nbbo_ask": 10.01, "canceled": False, "trade_code": None, "market_center": "L"},
        {"tracking_id": 3, "executed_at": "2026-10-16T14:02:00Z", "ticker": "XYZ", "size": 15000, "price": 10.02,
         "premium": 150300.0, "nbbo_bid": 10.01, "nbbo_ask": 10.03, "canceled": False, "trade_code": None, "market_center": "L"},
        {"tracking_id": 4, "executed_at": "2026-10-16T14:03:00Z", "ticker": "XYZ", "size": 999, "price": 10.50,
         "premium": 10489.5, "nbbo_bid": 10.00, "nbbo_ask": 10.04, "canceled": True, "trade_code": None, "market_center": "L"},
        {"tracking_id": 5, "executed_at": "2026-10-16T14:04:00Z", "ticker": "XYZ", "size": 5000, "price": 9.98,
         "premium": 49900.0, "nbbo_bid": 9.97, "nbbo_ask": 9.99, "canceled": False, "trade_code": "derivative_priced", "market_center": "L"},
    ]
    agg = reduce_prints(prints, "XYZ", cfg)
    assert agg["print_count"] == 3 and agg["block_count"] == 1
    checks += 1
    assert agg["total_premium"] == 1000.0 + 1998.0 + 150300.0
    checks += 1
    assert agg["buy_premium"] == 1000.0 + 150300.0 and agg["sell_premium"] == 1998.0
    checks += 1
    assert agg["net_premium"] > 0 and agg["accumulating"] is True
    checks += 1
    assert agg["top_level"] == 10.02 and agg["top_level_premium"] == 150300.0
    checks += 1
    assert agg["window_start"] == "2026-10-16T14:00:00Z" and agg["window_end"] == "2026-10-16T14:02:00Z"
    checks += 1
    assert agg["last_print_price"] == 10.02
    checks += 1
    assert agg["above_close_premium"] == 0.0 and agg["below_close_premium"] == 2998.0
    checks += 1
    cfg_all = DarkPoolConfig(include_derivative=True)
    assert reduce_prints(prints, "XYZ", cfg_all)["print_count"] == 4
    checks += 1
    payload = opportunity_payload(agg, "2026-10-17T00:00:00Z")
    assert abs(payload["dp_net_mm"] - 0.1493) < 1e-9 and payload["dp_blocks"] == 1 and payload["dp_accumulating"] is True
    checks += 1
    row = print_row(prints[0])
    assert row and row["tracking_id"] == 1 and row["price"] == 10.00 and row["size"] == 100
    checks += 1
    assert print_row({"tracking_id": None, "executed_at": "2026-10-16T14:00:00Z", "ticker": "XYZ"}) is None
    checks += 1
    row_codes = print_row({**prints[0], "sale_cond_codes": ["average_price_trade"]})
    assert row_codes["sale_cond_codes"] == '["average_price_trade"]'
    checks += 1
    return {"ok": True, "checks": checks, "model_version": MODEL_VERSION}


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False))
        return 0
    try:
        report, output = execute(args)
    except RunnerError as exc:
        logging.error("workflow failed: %s", exc)
        return 2
    print(json.dumps({
        "ok": True,
        "model_version": MODEL_VERSION,
        "run_id": report.run_id,
        "opportunity_date": report.opportunity_date,
        "input_symbols": report.input_symbols,
        "processed_symbols": report.processed_symbols,
        "failed_symbols": len(report.failed_symbols),
        "output": str(output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
