#!/usr/bin/env python3
"""Discover UW momentum candidates and safely stage them in the Supabase watchlist.

Dry-run is the default. Live watchlist changes require --apply.
The script uses only stock-market fields for discovery and does not score options flow.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

UW_DEFAULT_BASE = "https://api.unusualwhales.com"
YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_QUERY = (
    "@stock where price >= 10 and marketcap >= 1B and avg30_stock_volume >= 500K "
    "and one_year_close > 0 and one_month_close > 0 "
    "and ((one_month_close / one_year_close) - 1) >= 10% "
    "and distance_to_sma_50 >= 0 and distance_to_sma_50 <= 8% "
    "and distance_to_sma_200 >= 0 and week_52_high > 0 "
    "and price >= week_52_high * 0.75"
)
WATCHLIST_TABLE_DEFAULT = "watchlist"
PAGE_SIZE_DEFAULT = 500
SUPABASE_PAGE_SIZE = 1000
TIMEOUT_DEFAULT = 35
USER_AGENT = "UWStockScreener/1.0 (+https://github.com/rhcfok/ai-equity-trader-v2)"

# Normalize common exchange names, Yahoo codes and MICs into the exchange labels
# already used by the existing watchlist workflows. Generic country labels are
# deliberately excluded; Yahoo metadata must resolve those to a real venue.
EXCHANGE_ALIASES: dict[str, str] = {}
for _canonical, _aliases in {
    "NASDAQ": ("NASDAQ", "NASDAQGS", "NASDAQGM", "NASDAQCM", "NASDAQ GLOBAL SELECT", "NASDAQ GLOBAL MARKET", "NASDAQ CAPITAL MARKET", "NMS", "NGM", "NCM", "XNAS", "XNGS", "XNGM", "XNCM"),
    "NYSE": ("NYSE", "NEW YORK STOCK EXCHANGE", "NYQ", "XNYS"),
    "NYSE American": ("NYSE AMERICAN", "NYSEAMERICAN", "NYSE MKT", "AMEX", "ASE", "XASE"),
    "NYSE Arca": ("NYSE ARCA", "NYSEARCA", "ARCA", "PCX", "ARCX"),
    "Cboe": ("CBOE", "CBOE BZX", "BZX", "BATS", "EDGX", "EDGA", "XBX"),
    "IEX": ("IEX", "IEXG", "X IEX"),
}.items():
    for _alias in _aliases:
        EXCHANGE_ALIASES[_alias] = _canonical

PROTECTED_CATEGORIES = {"core", "satellite", "watch1", "hedge", "hedges"}


class RunnerError(RuntimeError):
    """Invalid configuration or an external data/API failure."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("$")


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def canonical_us_exchange(value: Any) -> str | None:
    """Return a canonical U.S. exchange for an explicitly recognized venue."""
    if not value:
        return None
    text = re.sub(r"[^A-Z0-9]+", " ", str(value).upper()).strip()
    compact = text.replace(" ", "")
    if text in EXCHANGE_ALIASES:
        return EXCHANGE_ALIASES[text]
    if compact in EXCHANGE_ALIASES:
        return EXCHANGE_ALIASES[compact]
    # Exchange descriptions vary across providers; match venue names after exact codes.
    if "NASDAQ" in text:
        return "NASDAQ"
    if "NYSE AMERICAN" in text or "NYSE MKT" in text or text == "AMEX":
        return "NYSE American"
    if "NYSE ARCA" in text or text == "ARCA":
        return "NYSE Arca"
    if "NEW YORK STOCK EXCHANGE" in text or text == "NYSE":
        return "NYSE"
    if "CBOE" in text or "BATS" in text or text in {"BZX", "EDGX", "EDGA"}:
        return "Cboe"
    if text == "IEX" or text == "IEXG":
        return "IEX"
    return None


def _request_json(url: str, headers: dict[str, str], timeout: int = TIMEOUT_DEFAULT, retries: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 429 and attempt < retries:
                try:
                    delay = min(10.0, max(0.5, float(exc.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1.0
                time.sleep(delay)
                last_error = exc
                continue
            if exc.code >= 500 and attempt < retries:
                time.sleep(0.5 * attempt)
                last_error = exc
                continue
            raise RunnerError(f"HTTP {exc.code} for {url.split('?')[0]}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * attempt)
                continue
            break
    raise RunnerError(f"Request failed for {url.split('?')[0]}: {last_error}")


def uw_get(path: str, params: dict[str, Any], api_key: str, base_url: str, client_api_id: str, timeout: int) -> Any:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{base_url.rstrip('/')}{path}?{query}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "UW-CLIENT-API-ID": client_api_id,
        "User-Agent": USER_AGENT,
    }
    return _request_json(url, headers, timeout=timeout)


def extract_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("results")
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("results")
    else:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RunnerError(f"Unexpected {label} response shape; expected an array of rows")
    return rows


def fetch_screener_candidates(
    api_key: str,
    base_url: str,
    query: str,
    page_size: int,
    max_pages: int,
    client_api_id: str,
    timeout: int,
) -> tuple[list[dict[str, Any]], bool]:
    collected: list[dict[str, Any]] = []
    truncated = False
    for page in range(max_pages):
        params = {
            "query": query,
            "issue_types[]": ["Common Stock"],
            "order": "one_year_perc",
            "order_direction": "desc",
            "limit": page_size,
            "offset": page,
        }
        payload = uw_get("/api/screener/stocks", params, api_key, base_url, client_api_id, timeout)
        rows = extract_rows(payload, "UW stock screener")
        collected.extend(rows)
        if len(rows) < page_size:
            break
        if page == max_pages - 1:
            truncated = True
    # De-duplicate by ticker while retaining the first row in UW's requested order.
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in collected:
        symbol = normalize_symbol(row.get("ticker") or row.get("symbol") or row.get("underlying_symbol"))
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(row)
    return output, truncated


def yahoo_exchange(symbol: str, timeout: int) -> tuple[str | None, str | None]:
    url = f"{YAHOO_CHART_BASE}/{urllib.parse.quote(symbol, safe='')}?range=5d&interval=1d"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    payload = _request_json(url, headers, timeout=timeout, retries=2)
    results = (payload.get("chart", {}).get("result") or []) if isinstance(payload, dict) else []
    if not results or not isinstance(results[0], dict):
        return None, None
    meta = results[0].get("meta") or {}
    for key in ("exchangeName", "fullExchangeName", "exchange"):
        value = meta.get(key)
        exchange = canonical_us_exchange(value)
        if exchange:
            return exchange, str(value)
    return None, None


def resolve_candidate_exchange(row: dict[str, Any], timeout: int) -> tuple[str | None, str | None, str | None]:
    symbol = normalize_symbol(row.get("ticker") or row.get("symbol") or row.get("underlying_symbol"))
    for key in ("exchange", "primary_exchange", "mic", "full_exchange_name"):
        raw = row.get(key)
        if raw:
            exchange = canonical_us_exchange(raw)
            if exchange:
                return exchange, f"UW:{key}", None
            # Generic country labels do not establish an exchange; ask Yahoo for
            # its listing metadata. Other explicit unknown venues are rejected.
            if str(raw).strip().upper() not in {"US", "USA", "UNITED STATES"}:
                return None, None, f"UW {key} is not a recognized U.S. exchange: {raw}"
            break
    try:
        exchange, raw = yahoo_exchange(symbol, timeout)
    except RunnerError as exc:
        return None, None, f"Yahoo listing check unresolved: {exc}"
    if exchange:
        return exchange, f"Yahoo Finance:{raw}", None
    return None, None, "No explicitly recognized U.S. exchange in UW or Yahoo metadata"


def row_is_common_stock(row: dict[str, Any]) -> bool:
    value = row.get("issue_type") or row.get("issue_types")
    if value is None:
        # The API request itself is constrained to Common Stock.
        return True
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    normalized = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    return "common stock" in normalized


def momentum_ex_recent_month(row: dict[str, Any]) -> float | None:
    month_close = as_float(row.get("one_month_close"))
    year_close = as_float(row.get("one_year_close"))
    if month_close is None or year_close is None or year_close <= 0:
        return None
    return month_close / year_close - 1.0


def candidate_summary(row: dict[str, Any], exchange: str, exchange_source: str) -> dict[str, Any]:
    symbol = normalize_symbol(row.get("ticker") or row.get("symbol") or row.get("underlying_symbol"))
    momentum = momentum_ex_recent_month(row)
    return {
        "symbol": symbol,
        "exchange": exchange,
        "exchange_verified_by": exchange_source,
        "issue_type": row.get("issue_type") or "Common Stock (query filter)",
        "price": rounded(as_float(row.get("underlying_price") or row.get("price")), 4),
        "market_cap": rounded(as_float(row.get("marketcap")), 0),
        "avg_30d_stock_volume": rounded(as_float(row.get("avg30_stock_volume") or row.get("avg30_volume")), 0),
        "momentum_ex_latest_month_pct": rounded(momentum * 100 if momentum is not None else None, 2),
        "one_year_return_pct": rounded(as_float(row.get("one_year_perc")) * 100 if as_float(row.get("one_year_perc")) is not None else None, 2),
        "distance_to_sma_50_pct": rounded(as_float(row.get("distance_to_sma_50")) * 100 if as_float(row.get("distance_to_sma_50")) is not None else None, 2),
        "distance_to_sma_200_pct": rounded(as_float(row.get("distance_to_sma_200")) * 100 if as_float(row.get("distance_to_sma_200")) is not None else None, 2),
        "week_52_high": rounded(as_float(row.get("week_52_high")), 4),
        "rsi_14": rounded(as_float(row.get("rsi_14") or row.get("rsi")), 2),
        "adx_14": rounded(as_float(row.get("adx_14") or row.get("adx")), 2),
        "macd_histogram": rounded(as_float(row.get("macd_12_26_9_histogram") or row.get("macd_histogram")), 6),
        "stock_volume_vs_avg30": rounded(as_float(row.get("stock_volume_vs_avg30_volume")), 3),
        "earnings_dte": as_float(row.get("earnings_dte")),
    }


def read_watchlist(
    base_url: str,
    api_key: str,
    table: str,
    timeout: int,
) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise RunnerError("SUPABASE_WATCHLIST_TABLE contains invalid characters")
    root = base_url.rstrip("/")
    if root.endswith("/rest/v1"):
        root = root[: -len("/rest/v1")]
    if not root:
        raise RunnerError("SUPABASE_URL is required")
    output: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "select": "id,symbol,exchange,category",
            "order": "id.asc",
            "limit": SUPABASE_PAGE_SIZE,
            "offset": offset,
        })
        url = f"{root}/rest/v1/{table}?{params}"
        headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        rows = extract_rows(_request_json(url, headers, timeout=timeout), "Supabase watchlist")
        output.extend(rows)
        if len(rows) < SUPABASE_PAGE_SIZE:
            break
        offset += len(rows)
    return output


def plan_watchlist_actions(candidates: list[dict[str, Any]], watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in watchlist:
        symbol = normalize_symbol(row.get("symbol"))
        if symbol:
            by_symbol[symbol].append(row)
    actions: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = candidate["symbol"]
        matches = sorted(by_symbol.get(symbol, []), key=lambda item: int(item.get("id") or 0))
        protected = [row for row in matches if str(row.get("category") or "").strip().lower() in PROTECTED_CATEGORIES]
        if protected:
            chosen = sorted(
                protected,
                key=lambda item: ({"core": 0, "satellite": 1, "watch1": 2, "hedge": 3, "hedges": 3}.get(str(item.get("category") or "").strip().lower(), 4), int(item.get("id") or 0)),
            )[0]
            actions.append({
                "symbol": symbol,
                "action": "keep",
                "category": str(chosen.get("category") or ""),
                "watchlist_id": chosen.get("id"),
                "reason": "Existing protected category; no change",
            })
        elif matches:
            chosen = matches[0]
            actions.append({
                "symbol": symbol,
                "action": "promote",
                "category_before": str(chosen.get("category") or ""),
                "category_after": "watch1",
                "watchlist_id": chosen.get("id"),
                "reason": "Existing lower-tier row promoted to watch1",
            })
        else:
            actions.append({
                "symbol": symbol,
                "action": "add",
                "category_after": "watch1",
                "reason": "New qualifying U.S.-listed common stock",
            })
    return actions


def apply_action(action: dict[str, Any], root: str, api_key: str, table: str, timeout: int) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise RunnerError("SUPABASE_WATCHLIST_TABLE contains invalid characters")
    root = root.rstrip("/")
    if root.endswith("/rest/v1"):
        root = root[: -len("/rest/v1")]
    url = f"{root}/rest/v1/{table}"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
        "User-Agent": USER_AGENT,
    }
    if action["action"] == "promote":
        record_id = action.get("watchlist_id")
        if record_id is None:
            raise RunnerError(f"Cannot promote {action['symbol']}: missing watchlist row id")
        url += "?" + urllib.parse.urlencode({"id": f"eq.{record_id}"})
        method = "PATCH"
        body = {"category": "watch1"}
    elif action["action"] == "add":
        method = "POST"
        body = {"symbol": action["symbol"], "exchange": action["exchange"], "category": "watch1"}
    else:
        return {**action, "write_status": "not_needed"}
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:400]
        raise RunnerError(f"Supabase {method} failed for {action['symbol']} (HTTP {exc.code}): {details}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RunnerError(f"Supabase {method} failed for {action['symbol']}: {exc}") from exc
    if method == "PATCH" and isinstance(payload, list) and not payload:
        raise RunnerError(f"Supabase promotion matched no row for {action['symbol']} (id={action.get('watchlist_id')})")
    return {**action, "write_status": "applied", "written_rows": len(payload) if isinstance(payload, list) else None}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover UW momentum stocks and prepare watch1 intake actions.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="UW screener DSL (default: JLaw-inspired liquid momentum discovery screen).")
    parser.add_argument("--output", type=Path, help="Audit JSON path (default: out/uw_stock_screener_<UTC-date>.json).")
    parser.add_argument("--apply", action="store_true", help="Apply add/promote actions to Supabase. Without this flag the run is read-only.")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT, help="UW page size (1-500).")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum UW result pages; a capped result set is flagged and not applied.")
    parser.add_argument("--timeout", type=int, default=int(env("REQUEST_TIMEOUT_SEC", str(TIMEOUT_DEFAULT))))
    parser.add_argument("--uw-base", default=env("UW_BASE", UW_DEFAULT_BASE))
    parser.add_argument("--watchlist-table", default=env("SUPABASE_WATCHLIST_TABLE", WATCHLIST_TABLE_DEFAULT))
    parser.add_argument("--client-api-id", default=env("UW_CLIENT_API_ID", "100001"))
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    uw_key = env("UW_API_KEY") or env("UNUSUAL_WHALES_API_KEY")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SERVICE_ROLE_KEY") or env("SUPABASE_KEY")
    if not uw_key:
        raise RunnerError("Set UW_API_KEY or UNUSUAL_WHALES_API_KEY in the process environment")
    if not supabase_url or not supabase_key:
        raise RunnerError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) to match candidates against the watchlist")
    if not 1 <= args.page_size <= 500:
        raise RunnerError("--page-size must be between 1 and 500")
    if not 1 <= args.max_pages <= 1000:
        raise RunnerError("--max-pages must be between 1 and 1000")
    if args.timeout < 1:
        raise RunnerError("--timeout must be positive")

    raw_rows, truncated = fetch_screener_candidates(
        uw_key, args.uw_base, args.query, args.page_size, args.max_pages,
        args.client_api_id, args.timeout,
    )
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for row in raw_rows:
        symbol = normalize_symbol(row.get("ticker") or row.get("symbol") or row.get("underlying_symbol"))
        if not symbol:
            rejected.append({"symbol": "", "reason": "UW row has no ticker/symbol"})
            continue
        if not row_is_common_stock(row):
            rejected.append({"symbol": symbol, "reason": f"Not a common stock: {row.get('issue_type') or row.get('issue_types')}"})
            continue
        exchange, source, reason = resolve_candidate_exchange(row, args.timeout)
        if not exchange:
            rejected.append({"symbol": symbol, "reason": reason or "U.S. listing could not be verified"})
            continue
        candidate = candidate_summary(row, exchange, source or "unknown")
        candidate["exchange"] = exchange
        accepted.append(candidate)

    watchlist = read_watchlist(supabase_url, supabase_key, args.watchlist_table, args.timeout)
    actions = plan_watchlist_actions(accepted, watchlist)
    by_symbol = {candidate["symbol"]: candidate for candidate in accepted}
    for action in actions:
        candidate = by_symbol[action["symbol"]]
        action["exchange"] = candidate["exchange"]
        action["candidate"] = candidate

    report: dict[str, Any] = {
        "provider": "Unusual Whales stock screener",
        "run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "query": args.query,
        "issue_types": ["Common Stock"],
        "result_count": len(raw_rows),
        "accepted_us_listings": len(accepted),
        "rejected_or_unverified_count": len(rejected),
        "result_set_truncated": truncated,
        "write_mode": "apply" if args.apply else "dry_run",
        "watchlist_table": args.watchlist_table,
        "candidates": accepted,
        "rejected_or_unverified": rejected,
        "watchlist_actions": actions,
        "write_errors": [],
    }

    if args.apply:
        if truncated:
            raise RunnerError("UW result set reached --max-pages; refusing to apply a potentially incomplete intake set. Increase --max-pages and rerun.")
        # Refresh right before mutation to avoid planning against a stale snapshot.
        fresh_watchlist = read_watchlist(supabase_url, supabase_key, args.watchlist_table, args.timeout)
        actions = plan_watchlist_actions(accepted, fresh_watchlist)
        for action in actions:
            candidate = by_symbol[action["symbol"]]
            action["exchange"] = candidate["exchange"]
            action["candidate"] = candidate
        applied: list[dict[str, Any]] = []
        for action in actions:
            try:
                applied.append(apply_action(action, supabase_url, supabase_key, args.watchlist_table, args.timeout))
            except RunnerError as exc:
                failed = {**action, "write_status": "failed", "error": str(exc)}
                applied.append(failed)
                report["write_errors"].append({"symbol": action["symbol"], "error": str(exc)})
        report["watchlist_actions"] = applied
        report["writes_applied"] = sum(1 for item in applied if item.get("write_status") == "applied")
        report["writes_failed"] = len(report["write_errors"])
    else:
        report["writes_applied"] = 0
        report["writes_failed"] = 0
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.output or Path(f"out/uw_stock_screener_{dt.datetime.now(dt.timezone.utc).date().isoformat()}.json")
    try:
        report = run(args)
        write_json(output, report)
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": report["writes_failed"] == 0,
        "result_count": report["result_count"],
        "accepted_us_listings": report["accepted_us_listings"],
        "rejected_or_unverified_count": report["rejected_or_unverified_count"],
        "planned_actions": len(report["watchlist_actions"]),
        "writes_applied": report["writes_applied"],
        "writes_failed": report["writes_failed"],
        "result_set_truncated": report["result_set_truncated"],
        "output": str(output),
    }, indent=2))
    return 2 if report["writes_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
