#!/usr/bin/env python3
"""Compute unsigned GEX snapshots from Unusual Whales contract data.

The calculation deliberately uses Unusual Whales option-contract rows, not the
provider's signed/assumed dealer-GEX endpoints. It preserves the established
workflow rules: 0-30 DTE, nearest expiry per strike, min OI 50, moneyness
+/-20%, $1M sub-resolution, and side-of-spot walls.

Required environment variables:
  UW_API_KEY (or UNUSUAL_WHALES_API_KEY)
  SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (unless DRY_RUN=true)

No secret is read from files, logged, or written to the database.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(level: str, message: str) -> None:
    sys.stderr.write(f"[{level}] {message}\n")
    sys.stderr.flush()


def die(message: str, code: int = 1) -> None:
    log("ERROR", message)
    print(json.dumps({"ok": False, "provider": "unusual-whales", "error": message}))
    raise SystemExit(code)


class WorkflowError(RuntimeError):
    """A data-source, schema, or calculation precondition failure."""


SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")].rstrip("/")
SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
UW_API_KEY = env("UW_API_KEY") or env("UNUSUAL_WHALES_API_KEY")
UW_BASE = env("UW_BASE", "https://api.unusualwhales.com").rstrip("/")
UW_CLIENT_API_ID = env("UW_CLIENT_API_ID", "100001")
SNAPSHOT_TABLE = env("SNAPSHOT_TABLE", "gex_snapshot")
WATCHLIST_TABLE = env("SUPABASE_WATCHLIST_TABLE", "watchlist")
WATCHLIST_CATEGORIES = tuple(
    item.strip().lower()
    for item in env("UW_WATCHLIST_CATEGORIES", "core,satellite,watch1").split(",")
    if item.strip()
)
MAX_WATCHLIST_SYMBOLS = int(env("UW_MAX_WATCHLIST_SYMBOLS", "200"))
EXPLICIT_SYMBOLS = [
    item.strip().upper()
    for item in env("BRIEFING_SYMBOLS").split(",")
    if item.strip()
]
DTE_FILTER = env("UW_DTE_FILTER", "0,30")
BATCH_SIZE = int(env("BATCH_SIZE", "1"))
TIMEOUT = int(env("REQUEST_TIMEOUT_SEC", "45"))
RUN_LABEL = env("RUN_LABEL", "manual")
MIN_OI = int(env("GEX_MIN_OI", "50"))
MONEYNESS_PCT = float(env("GEX_MONEYNESS_PCT", "20"))
RESOLUTION_MM = float(env("GEX_RESOLUTION_MM", "1.0"))
DRY_RUN = env("DRY_RUN").lower() in {"1", "true", "yes", "on"}
US_EXCHANGES = {
    "NASDAQ", "NASDAQGS", "NASDAQCM", "NASDAQGM", "NYSE", "NYSEARCA",
    "NYSE AMERICAN", "NYSE MKT", "AMEX", "ARCA", "BATS", "CBOE", "IEX",
    "US", "USA",
}


def uw_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a documented Unusual Whales endpoint with bounded 429 retries."""
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
    url = f"{UW_BASE}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {UW_API_KEY}")
    request.add_header("UW-CLIENT-API-ID", UW_CLIENT_API_ID)
    request.add_header("User-Agent", "GexWorkflow/2.0")
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 429 and attempt < 3:
                retry_after = float(exc.headers.get("Retry-After", "1"))
                log("WARN", f"Unusual Whales 429; sleeping {retry_after}s (attempt {attempt}/3)")
                time.sleep(retry_after)
                continue
            raise WorkflowError(f"Unusual Whales HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
            if attempt < 3:
                time.sleep(0.5 * attempt)
                continue
            raise WorkflowError(f"Unusual Whales request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WorkflowError("Unusual Whales returned invalid JSON") from exc
    raise WorkflowError("Unusual Whales request retry budget exhausted")


def data_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else payload if isinstance(payload, list) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise WorkflowError(f"Unexpected {label} response shape")
    return rows


def as_date(value: Any, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise WorkflowError(f"Invalid {label}: {value!r}") from exc


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("$")


def is_us_listing(row: dict[str, Any]) -> bool:
    """Accept only explicitly identified US exchange listings."""
    venue = str(row.get("exchange") or row.get("primary_exchange") or row.get("mic") or "").strip().upper()
    return venue in US_EXCHANGES or venue.startswith("NASDAQ") or venue.startswith("NYSE")


def prepare_watchlist_symbols(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Order requested categories, require US listings, and deduplicate symbols."""
    if not WATCHLIST_CATEGORIES:
        raise WorkflowError("UW_WATCHLIST_CATEGORIES must contain at least one category")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for category in WATCHLIST_CATEGORIES:
        for row in rows:
            row_category = str(row.get("category") or "").strip().lower()
            symbol = normalize_symbol(row.get("symbol") or row.get("ticker"))
            if row_category != category or not symbol or symbol in seen or not is_us_listing(row):
                continue
            seen.add(symbol)
            output.append({"symbol": symbol, "category": category})
            if len(output) >= MAX_WATCHLIST_SYMBOLS:
                return output
    return output


def load_watchlist_symbols() -> list[dict[str, str]]:
    """Read the configured categories from Supabase's source-of-truth watchlist."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", WATCHLIST_TABLE):
        raise WorkflowError("SUPABASE_WATCHLIST_TABLE contains invalid characters")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise WorkflowError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for watchlist source")
    category_filter = "in.(" + ",".join(WATCHLIST_CATEGORIES) + ")"
    query = urllib.parse.urlencode({
        "select": "id,symbol,exchange,category",
        "category": category_filter,
        "order": "id.asc",
        "limit": str(max(MAX_WATCHLIST_SYMBOLS * 5, 500)),
    })
    request = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{WATCHLIST_TABLE}?{query}")
    request.add_header("apikey", SUPABASE_KEY)
    request.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise WorkflowError(f"Supabase watchlist HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Supabase watchlist request failed: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise WorkflowError("Supabase watchlist response was not an array of rows")
    symbols = prepare_watchlist_symbols(payload)
    if not symbols:
        raise WorkflowError("No US-listed symbols found in configured watchlist categories")
    log("INFO", f"watchlist table={WATCHLIST_TABLE} raw_rows={len(payload)} accepted_us_symbols={len(symbols)} categories={','.join(WATCHLIST_CATEGORIES)}")
    return symbols


def select_expiries(rows: list[dict[str, Any]], dte_min: int, dte_max: int) -> tuple[str, list[dict[str, Any]]]:
    """Select a consistent market-date set of expiry rows in the DTE window."""
    market_dates = {str(row.get("date") or "")[:10] for row in rows if row.get("date")}
    if len(market_dates) != 1:
        raise WorkflowError("Greek-exposure-by-expiry returned no single market date")
    trade_date = next(iter(market_dates))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        expiry = str(row.get("expiry") or "")[:10]
        try:
            dte = int(row.get("dte"))
        except (TypeError, ValueError):
            continue
        if expiry and dte_min <= dte <= dte_max and expiry not in seen:
            selected.append({"expiry": expiry, "dte": dte})
            seen.add(expiry)
    selected.sort(key=lambda row: (row["dte"], row["expiry"]))
    if not selected:
        raise WorkflowError(f"No expiry rows inside DTE [{dte_min}, {dte_max}]")
    return trade_date, selected


def quote_spot(payload: Any, symbol: str) -> float:
    """Extract a positive latest-trade price, preferring the provider's last trade."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise WorkflowError(f"Unexpected quote response for {symbol}")
    candidates = [
        (data.get("last_trade") or {}).get("price") if isinstance(data.get("last_trade"), dict) else None,
        (data.get("quote_values") or {}).get("midpoint") if isinstance(data.get("quote_values"), dict) else None,
        (data.get("quote") or {}).get("last") if isinstance(data.get("quote"), dict) else None,
    ]
    for candidate in candidates:
        try:
            price = float(candidate)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    raise WorkflowError(
        f"Unusual Whales did not provide a usable underlying price for {symbol}; "
        "CBOE index pricing may require separate provisioning"
    )


OCC = re.compile(r"^(?P<root>[A-Z0-9.]+)(?P<date>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")


def normalize_contract(row: dict[str, Any], expiry: str, dte: int) -> dict[str, Any] | None:
    """Normalize documented option-contract fields, including OCC-symbol fallback."""
    side = str(row.get("option_type") or row.get("type") or "").lower()
    strike: Any = row.get("strike")
    option_symbol = str(row.get("option_symbol") or "").upper()
    if side not in {"call", "put"} or strike is None:
        match = OCC.fullmatch(option_symbol)
        if not match:
            return None
        side = "call" if match.group("side") == "C" else "put"
        strike = int(match.group("strike")) / 1000.0
    try:
        return {
            "strike": float(strike),
            "option_type": side,
            "open_interest": float(row.get("open_interest")),
            "gamma": float(row.get("gamma")),
            "dte": int(dte),
            "expiry": expiry,
        }
    except (TypeError, ValueError):
        return None


def fetch_unusual_whales(symbol: str) -> dict[str, Any]:
    """Fetch gamma/OI contracts and a matching snapshot-time quote for one symbol."""
    try:
        dte_min, dte_max = (int(value) for value in DTE_FILTER.split(","))
    except ValueError as exc:
        raise WorkflowError("UW_DTE_FILTER must be two integer values: min,max") from exc
    if dte_min < 0 or dte_max < dte_min:
        raise WorkflowError("UW_DTE_FILTER must be a non-negative increasing range")

    # Used as a DTE/market-date calendar only. Do not use its signed GEX values.
    expiry_payload = uw_get(f"/api/stock/{symbol}/greek-exposure/expiry")
    trade_date, expiries = select_expiries(data_rows(expiry_payload, "expiry metadata"), dte_min, dte_max)
    spot = quote_spot(uw_get(f"/api/stock/{symbol}/quote"), symbol)

    normalized: list[dict[str, Any]] = []
    raw_count = 0
    for expiry_item in expiries:
        expiry = expiry_item["expiry"]
        dte = int(expiry_item["dte"])
        page = 0
        while True:
            payload = uw_get(
                f"/api/stock/{symbol}/option-contracts",
                {"expiry": expiry, "limit": 500, "page": page},
            )
            contracts = data_rows(payload, f"option contracts {symbol} {expiry}")
            raw_count += len(contracts)
            for contract in contracts:
                parsed = normalize_contract(contract, expiry, dte)
                if parsed is not None:
                    normalized.append(parsed)
            if len(contracts) < 500:
                break
            page += 1
            if page > 100:
                raise WorkflowError(f"Unexpected pagination depth for {symbol} {expiry}")
    if not normalized:
        raise WorkflowError(f"No usable Unusual Whales gamma/OI contracts for {symbol}")
    return {
        "spot": spot,
        "rows": normalized,
        "raw_count": raw_count,
        "trade_date": trade_date,
        "expiries": [item["expiry"] for item in expiries],
    }


def aggregate(rows: list[dict[str, Any]], spot: float, trade_date: str | None = None) -> dict[str, Any]:
    """Calculate unsigned call/put magnitudes from contract-level gamma and OI."""
    if spot <= 0:
        raise WorkflowError("Spot must be positive")
    lo = (1.0 - MONEYNESS_PCT / 100.0) * spot
    hi = (1.0 + MONEYNESS_PCT / 100.0) * spot

    # One contract row per side becomes one strike/expiry row; then retain the
    # nearest expiry per strike so no multi-expiry summing occurs.
    grouped: dict[tuple[int, float], dict[str, float]] = {}
    for row in rows:
        try:
            strike = float(row["strike"])
            dte = int(row["dte"])
            gamma = float(row["gamma"])
            oi = float(row["open_interest"])
        except (KeyError, TypeError, ValueError):
            continue
        side = str(row.get("option_type") or "").lower()
        if side not in {"call", "put"} or gamma < 0 or oi < 0:
            continue
        item = grouped.setdefault((dte, strike), {"dte": float(dte), "strike": strike, "call_oi": 0.0, "put_oi": 0.0, "call_gamma": 0.0, "put_gamma": 0.0})
        item[f"{side}_oi"] = oi
        item[f"{side}_gamma"] = gamma

    nearest: dict[float, dict[str, float]] = {}
    for item in grouped.values():
        strike = item["strike"]
        if not lo <= strike <= hi or max(item["call_oi"], item["put_oi"]) < MIN_OI:
            continue
        if strike not in nearest or item["dte"] < nearest[strike]["dte"]:
            nearest[strike] = item

    coefficient = 100.0 * spot * 0.01 / 1_000_000.0
    calls = {strike: coefficient * item["call_gamma"] * item["call_oi"] for strike, item in nearest.items()}
    puts = {strike: -coefficient * item["put_gamma"] * item["put_oi"] for strike, item in nearest.items()}
    strikes = set(calls) | set(puts)

    call_wall, call_wall_gex = None, 0.0
    for strike, value in calls.items():
        if strike >= spot and value > call_wall_gex:
            call_wall, call_wall_gex = strike, value
    put_wall, put_wall_gex = None, 0.0
    for strike, value in puts.items():
        if strike <= spot and value < put_wall_gex:
            put_wall, put_wall_gex = strike, value

    net = sum(calls.values()) + sum(puts.values())
    absolute = sum(abs(value) for value in calls.values()) + sum(abs(value) for value in puts.values())
    regime = "sub_resolution" if abs(net) < RESOLUTION_MM else "positive" if net > 0 else "negative"
    gravity = "up" if call_wall is not None and (put_wall is None or call_wall_gex >= abs(put_wall_gex)) else "down" if put_wall is not None else "n/a"
    peak = max(strikes, key=lambda strike: abs(calls.get(strike, 0.0)) + abs(puts.get(strike, 0.0)), default=None)

    above, below = [], []
    for strike in strikes:
        entry = {"strike": strike, "gex_mm": calls.get(strike, 0.0) + puts.get(strike, 0.0), "distance_pct": (strike - spot) / spot * 100.0}
        (above if strike >= spot else below).append(entry)
    above.sort(key=lambda item: -abs(item["gex_mm"]))
    below.sort(key=lambda item: -abs(item["gex_mm"]))
    ladder = ([{"side": "above", **item} for item in above[:8]] + [{"side": "below", **item} for item in below[:7]])

    validity = ["ok"]
    if call_wall is None and put_wall is None:
        validity.append("walls_undefined")
    if regime == "sub_resolution":
        validity.append("sub_resolution")
    if trade_date:
        try:
            if as_date(trade_date, "trade date").weekday() >= 5:
                validity.append("weekend_tradedate")
        except WorkflowError:
            pass

    return {
        "spot_price": spot,
        "net_gex_mm": round(net, 4),
        "abs_gex_mm": round(absolute, 4),
        "zero_gamma": None,
        "peak_gex_strike": peak,
        "call_wall": call_wall,
        "call_wall_gex_mm": round(call_wall_gex, 4) if call_wall is not None else None,
        "put_wall": put_wall,
        "put_wall_gex_mm": round(put_wall_gex, 4) if put_wall is not None else None,
        "regime": regime,
        "pos_gex_above_spot_mm": round(sum(value for strike, value in calls.items() if strike >= spot), 4),
        "pos_gex_below_spot_mm": round(sum(calls.get(strike, 0.0) + puts.get(strike, 0.0) for strike in strikes if strike < spot), 4),
        "gravity": gravity,
        "gamma_ladder": [{"side": item["side"], "gex_mm": round(item["gex_mm"], 6), "strike": item["strike"], "distance_pct": round(item["distance_pct"], 3)} for item in ladder],
        "strike_count": len(strikes),
        "raw_rows": len(rows),
        "validity": validity,
        "signed_source": "none",
    }


def supabase_insert(row: dict[str, Any]) -> int:
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{SNAPSHOT_TABLE}",
        data=json.dumps([row], default=str).encode("utf-8"),
        method="POST",
    )
    request.add_header("apikey", SUPABASE_KEY)
    request.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Prefer", "return=minimal")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise WorkflowError(f"Supabase HTTP {exc.code}: {body}") from exc


def process_symbol(symbol: str) -> dict[str, Any]:
    fetched = fetch_unusual_whales(symbol)
    summary = aggregate(fetched["rows"], fetched["spot"], fetched.get("trade_date"))
    row = {
        "symbol": symbol,
        "snapshot_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spot_price": summary["spot_price"],
        "net_gex": round(summary["net_gex_mm"] * 1_000_000.0, 2),
        "net_gex_mm": summary["net_gex_mm"],
        "abs_gex_mm": summary["abs_gex_mm"],
        "zero_gamma": summary["zero_gamma"],
        "peak_gex_strike": summary["peak_gex_strike"],
        "call_wall": summary["call_wall"],
        "call_wall_gex_mm": summary["call_wall_gex_mm"],
        "put_wall": summary["put_wall"],
        "put_wall_gex_mm": summary["put_wall_gex_mm"],
        "regime": summary["regime"],
        "near_term_gex_pct": 100.0,
        "pos_gex_above_spot_mm": summary["pos_gex_above_spot_mm"],
        "pos_gex_below_spot_mm": summary["pos_gex_below_spot_mm"],
        "gravity": summary["gravity"],
        "gamma_ladder": summary["gamma_ladder"],
        "strike_count": summary["strike_count"],
        "raw_rows": summary["raw_rows"],
    }
    if not DRY_RUN:
        supabase_insert(row)
    row["_local"] = {"trade_date": fetched["trade_date"], "expiries": fetched["expiries"], "validity": summary["validity"], "signed_source": "none"}
    return row


def main() -> None:
    if not UW_API_KEY:
        die("UW_API_KEY or UNUSUAL_WHALES_API_KEY not set")
    if (not DRY_RUN or not EXPLICIT_SYMBOLS) and (not SUPABASE_URL or not SUPABASE_KEY):
        die("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    symbols = (
        [{"symbol": symbol, "category": "explicit"} for symbol in EXPLICIT_SYMBOLS]
        if EXPLICIT_SYMBOLS
        else load_watchlist_symbols()
    )
    source = "BRIEFING_SYMBOLS override" if EXPLICIT_SYMBOLS else f"{WATCHLIST_TABLE}:{','.join(WATCHLIST_CATEGORIES)}"
    log("INFO", f"provider=unusual-whales run_label={RUN_LABEL} source={source} symbols={len(symbols)} dte={DTE_FILTER} min_oi={MIN_OI} moneyness_pct={MONEYNESS_PCT} resolution_mm={RESOLUTION_MM} dry_run={DRY_RUN}")
    processed: list[str] = []
    failed: list[dict[str, str]] = []
    for index, watch_item in enumerate(symbols, 1):
        symbol = watch_item["symbol"]
        try:
            row = process_symbol(symbol)
            processed.append(symbol)
            local = row["_local"]
            log("INFO", f"[{index}/{len(symbols)}] {symbol} category={watch_item['category']} spot={row['spot_price']:.2f} regime={row['regime']} cw={row['call_wall']} pw={row['put_wall']} gx={row['net_gex_mm']:+.2f}M strikes={row['strike_count']} trade_date={local['trade_date']} validity={local['validity']}")
        except Exception as exc:
            failed.append({"symbol": symbol, "error": str(exc)[:300]})
            log("ERROR", f"[{index}/{len(symbols)}] {symbol} category={watch_item['category']} FAILED: {exc}")
        if BATCH_SIZE and index % max(1, BATCH_SIZE) == 0:
            time.sleep(0.25)
    print(json.dumps({"ok": True, "provider": "unusual-whales", "source": source, "categories": list(WATCHLIST_CATEGORIES), "dry_run": DRY_RUN, "processed": len(processed), "failed_count": len(failed), "failed": failed, "run_label": RUN_LABEL}))


if __name__ == "__main__":
    main()
