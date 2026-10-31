#!/usr/bin/env python3
"""Read-only CSP and 10-point bull-put-spread opportunity runner.

Inputs
------
* Supabase: trade-jlaw-v2, gex_snapshot, quant2_leaderboard, watchlist
* Unusual Whales: /api/stock/{ticker}/option-contracts (NBBO, IV, Greeks, OI)
* Optional earnings JSON from the connected Unusual Whales upcoming-earnings tool

The runner never creates an order, trade instruction, database row, or schedule.
It writes an audit JSON and a concise Markdown research report only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from csp_bps_opportunity_core import ScreeningConfig, ScreeningError, build_audit, is_us_exchange, markdown_report


class RunnerError(RuntimeError):
    """Raised for missing credentials, malformed source data, or failed requests."""


def env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RunnerError(f"Invalid ISO date: {value}") from exc


def json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def json_read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"Could not read JSON input {path}: {type(exc).__name__}") from exc


def api_rows(payload: Any, label: str) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else payload if isinstance(payload, list) else None
    if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
        raise RunnerError(f"Unexpected {label} response shape.")
    return rows


class HttpJsonClient:
    def __init__(self, base_url: str, headers: dict[str, str], timeout: int = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None, *, retries: int = 3) -> Any:
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None], doseq=True
        )
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        for attempt in range(1, retries + 1):
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code in {429, 503} and attempt < retries:
                    retry_after = float(exc.headers.get("Retry-After", "1"))
                    time.sleep(max(0.25, retry_after))
                    continue
                raise RunnerError(f"GET {path} HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < retries:
                    time.sleep(0.5 * attempt)
                    continue
                raise RunnerError(f"GET {path} failed: {type(exc).__name__}") from exc
        raise RunnerError(f"GET {path} exhausted retry budget")


class SupabaseReader:
    """Read-only PostgREST adapter; there are no write methods by design."""

    def __init__(self, url: str, key: str) -> None:
        rest = url.rstrip("/")
        if not rest.endswith("/rest/v1"):
            rest = f"{rest}/rest/v1"
        self.http = HttpJsonClient(rest, {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})

    def select(self, table: str, *, select: str, order: str, limit: int, extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
        params = {"select": select, "order": order, "limit": str(limit), **(extra or {})}
        return api_rows(self.http.get(f"/{table}", params), table)


class UnusualWhalesReader:
    def __init__(self, api_key: str, base_url: str, client_id: str) -> None:
        self.http = HttpJsonClient(
            base_url,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "UW-CLIENT-API-ID": client_id,
                "User-Agent": "CspBpsOpportunityRunner/1.0",
            },
        )

    def option_contracts(self, symbol: str, min_dte: int, max_dte: int) -> dict[str, Any]:
        path = f"/api/stock/{urllib.parse.quote(symbol)}/option-contracts"
        params = {
            "option_type": "put",
            "maybe_otm_only": "true",
            "exclude_zero_oi_chains": "true",
            "min_dte": str(min_dte),
            "max_dte": str(max_dte),
            "limit": "500",
        }
        payload = self.http.get(path, {**params, "page": "0"})
        if not isinstance(payload, dict):
            raise RunnerError(f"Unusual Whales chain for {symbol} is not an object.")
        row_key = next((key for key in ("data", "states", "chains") if isinstance(payload.get(key), list)), None)
        if row_key is None:
            return payload
        rows = payload[row_key]
        if not all(isinstance(row, dict) for row in rows):
            raise RunnerError(f"Unusual Whales chain for {symbol} has non-object rows.")
        # The API caps a page at 500 contracts. Walk all pages so far-OTM legs are
        # not silently excluded from a hard strike-safety scan.
        page = 1
        while len(rows) == 500:
            next_payload = self.http.get(path, {**params, "page": str(page)})
            next_rows = next_payload.get(row_key) if isinstance(next_payload, dict) else None
            if not isinstance(next_rows, list) or not all(isinstance(row, dict) for row in next_rows):
                raise RunnerError(f"Unusual Whales chain page {page} for {symbol} has an unexpected shape.")
            rows.extend(next_rows)
            if len(next_rows) < 500:
                break
            page += 1
            if page > 100:
                raise RunnerError(f"Unusual Whales chain pagination exceeded 100 pages for {symbol}.")
        return payload


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def newest_rows(rows: list[dict[str, Any]], key: str, timestamp_field: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "").upper().strip()
        if not value:
            continue
        prior = output.get(value)
        if prior is None or str(row.get(timestamp_field) or "") > str(prior.get(timestamp_field) or ""):
            output[value] = row
    return output


def preferred_jlaw(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Use latest run date, prefer chart row, then newest immutable ID."""
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        prior = output.get(symbol)
        sort_key = (str(row.get("run_date") or ""), int(row.get("jlaw_review_type") == "chart"), int(row.get("id") or 0))
        prior_key = (
            str(prior.get("run_date") or ""),
            int(prior.get("jlaw_review_type") == "chart"),
            int(prior.get("id") or 0),
        ) if prior else ("", 0, 0)
        if prior is None or sort_key > prior_key:
            output[symbol] = row
    return output


def load_earnings(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load connector-exported upcoming earnings in mapping or list form."""
    if path is None:
        return {}
    payload = json_read(path)
    output: dict[str, dict[str, Any]] = {}
    if isinstance(payload, dict):
        source = payload.get("data") if isinstance(payload.get("data"), list) else payload
        if isinstance(source, dict):
            for symbol, value in source.items():
                if isinstance(value, dict):
                    output[str(symbol).upper()] = value
                elif isinstance(value, str):
                    output[str(symbol).upper()] = {"date": value}
        elif isinstance(source, list):
            payload = source
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ticker") or row.get("symbol") or "").upper()
            if symbol:
                output[symbol] = row
    return output


def collect_contexts(reader: SupabaseReader, symbols: list[str] | None, include_watch: bool, max_symbols: int, earnings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Build US-only underlying contexts from the existing four read-only tables."""
    jlaw_rows = reader.select(
        "trade-jlaw-v2",
        select="id,run_date,symbol,classification,jlaw_review_type,jlaw_score,current_price,entry_price,stop_loss,option_iv_rank,option_iv30,option_rv20,option_score,option_tier",
        order="run_date.desc,id.desc",
        limit=max(1000, max_symbols * 20),
        extra={"classification": "in.(Valid,Watch)"},
    )
    jlaw = preferred_jlaw(jlaw_rows)
    watchlist_rows = reader.select("watchlist", select="symbol,exchange", order="id.asc", limit=2000)
    exchange = {str(row.get("symbol") or "").upper(): row.get("exchange") for row in watchlist_rows}
    requested = {symbol.upper() for symbol in symbols} if symbols else None
    allowed_classes = {"valid", "watch"} if include_watch else {"valid"}
    selected = [
        symbol for symbol, row in jlaw.items()
        if (requested is None or symbol in requested)
        and str(row.get("classification") or "").lower() in allowed_classes
        and is_us_exchange(exchange.get(symbol))
    ]
    selected.sort(key=lambda item: (-float(jlaw[item].get("jlaw_score") or 0), item))
    selected = selected[:max_symbols]
    if not selected:
        return []

    gex_rows: list[dict[str, Any]] = []
    quant_rows: list[dict[str, Any]] = []
    for part in chunked(selected, 25):
        expression = "in.(" + ",".join(part) + ")"
        gex_rows.extend(reader.select(
            "gex_snapshot",
            select="symbol,snapshot_ts,spot_price,put_wall,put_wall_gex_mm,call_wall,regime,net_gex_mm,abs_gex_mm",
            order="snapshot_ts.desc,id.desc",
            limit=1000,
            extra={"symbol": expression},
        ))
        quant_rows.extend(reader.select(
            "quant2_leaderboard",
            select="ticker,as_of,rank,score_hist_freq,n_active_rules,active_rules",
            order="as_of.desc,id.desc",
            limit=1000,
            extra={"ticker": expression},
        ))
    gex = newest_rows(gex_rows, "symbol", "snapshot_ts")
    quant = newest_rows(quant_rows, "ticker", "as_of")
    return [
        {
            "symbol": symbol,
            "exchange": exchange.get(symbol),
            "jlaw": jlaw[symbol],
            "gex": gex.get(symbol, {}),
            "quant": quant.get(symbol, {}),
            "earnings": earnings.get(symbol, {}),
        }
        for symbol in selected
    ]


def collect_live_chains(reader: UnusualWhalesReader, contexts: list[dict[str, Any]], config: ScreeningConfig) -> tuple[dict[str, Any], list[dict[str, str]]]:
    chains: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    min_dte = min(config.csp_min_dte, config.bps_min_dte)
    max_dte = max(config.csp_max_dte, config.bps_max_dte)
    for context in contexts:
        symbol = str(context["symbol"])
        try:
            chains[symbol] = reader.option_contracts(symbol, min_dte, max_dte)
        except RunnerError as exc:
            failures.append({"symbol": symbol, "error": str(exc)})
    return chains, failures


def fixture_inputs(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], date]:
    payload = json_read(path)
    if not isinstance(payload, dict):
        raise RunnerError("Fixture must be a JSON object.")
    contexts = payload.get("contexts") or payload.get("underlyings")
    chains = payload.get("chains")
    if not isinstance(contexts, list) or not all(isinstance(item, dict) for item in contexts):
        raise RunnerError("Fixture needs contexts/underlyings as an array of objects.")
    if not isinstance(chains, dict):
        raise RunnerError("Fixture needs chains as an object keyed by ticker.")
    fixture_date = parse_date(str(payload.get("as_of") or date.today().isoformat()))
    return contexts, chains, payload.get("earnings") if isinstance(payload.get("earnings"), dict) else {}, fixture_date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create read-only CSP and 10-point BPS research artifacts.")
    parser.add_argument("--output-dir", required=True, help="Directory for audit JSON and Markdown report.")
    parser.add_argument("--fixture", help="Offline deterministic JSON fixture. Skips all network access.")
    parser.add_argument("--symbols", help="Optional comma-separated symbols. Each must be verified US-listed in watchlist.")
    parser.add_argument("--earnings-json", help="Export from Unusual Whales upcoming-earnings tool; required by default for qualifying candidates.")
    parser.add_argument("--as-of", help="ISO market/reference date. Defaults to the current calendar date.")
    watch_scope = parser.add_mutually_exclusive_group()
    watch_scope.add_argument("--valid-only", dest="include_watch", action="store_false", help="Restrict the screen to J Law Valid rows.")
    watch_scope.add_argument("--include-watch", dest="include_watch", action="store_true", help="Include J Law Watch rows (the default).")
    parser.set_defaults(include_watch=True)
    parser.add_argument("--allow-unconfirmed-earnings", action="store_true", help="Publish candidates with an explicit earnings-unconfirmed caution.")
    parser.add_argument("--bps-width", type=float, default=10.0, help="Bull-put-spread width in points. Default: 10.")
    parser.add_argument("--min-open-interest", type=int, default=100, help="Per-leg minimum OI. Default: 100.")
    parser.add_argument("--max-bid-ask-spread-pct", type=float, default=0.25, help="Maximum NBBO width/midpoint. Default: 0.25.")
    parser.add_argument("--top-n", type=int, default=20, help="Maximum number of hard-pass structures to publish, ranked across CSP and BPS. Default: 20.")
    parser.add_argument("--max-symbols", type=int, default=1000, help="Maximum US underlyings to query. Default: 1000 (effectively the full Valid/Watch pool; pass a smaller number to cap).")
    parser.add_argument("--exit-nonzero-on-provider-errors", action="store_true", help="Return non-zero when one or more chain requests fail.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.bps_width <= 0 or args.min_open_interest < 0 or args.max_symbols < 1 or args.top_n < 1:
        raise RunnerError("Width must be positive; OI non-negative; max-symbols and top-n at least one.")
    config = ScreeningConfig(
        bps_width=args.bps_width,
        min_open_interest=args.min_open_interest,
        max_bid_ask_spread_pct=args.max_bid_ask_spread_pct,
        require_earnings_date=not args.allow_unconfirmed_earnings,
        include_watch=args.include_watch,
        rank_limit=args.top_n,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    provider_failures: list[dict[str, str]] = []
    if args.fixture:
        contexts, chains, fixture_earnings, fixture_as_of = fixture_inputs(Path(args.fixture))
        as_of = parse_date(args.as_of) if args.as_of else fixture_as_of
        for context in contexts:
            symbol = str(context.get("symbol") or "").upper()
            if symbol and symbol in fixture_earnings and not context.get("earnings"):
                context["earnings"] = fixture_earnings[symbol]
    else:
        supabase_url = env("SUPABASE_URL")
        supabase_key = env("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
        uw_key = env("UW_API_KEY", "UNUSUAL_WHALES_API_KEY")
        if not supabase_url or not supabase_key:
            raise RunnerError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) are required.")
        if not uw_key:
            raise RunnerError("UW_API_KEY or UNUSUAL_WHALES_API_KEY is required for the direct Unusual Whales API.")
        earnings = load_earnings(Path(args.earnings_json)) if args.earnings_json else {}
        requested = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()] or None
        contexts = collect_contexts(SupabaseReader(supabase_url, supabase_key), requested, args.include_watch, args.max_symbols, earnings)
        uw = UnusualWhalesReader(uw_key, env("UW_BASE") or "https://api.unusualwhales.com", env("UW_CLIENT_API_ID") or "100001")
        chains, provider_failures = collect_live_chains(uw, contexts, config)
        as_of = parse_date(args.as_of) if args.as_of else date.today()

    audit = build_audit(contexts, chains, config, as_of)
    audit["provider_failures"] = provider_failures
    audit["sources"] = {
        "supabase_tables": ["trade-jlaw-v2", "gex_snapshot", "quant2_leaderboard", "watchlist"],
        "unusual_whales_endpoint": "/api/stock/{ticker}/option-contracts",
        "earnings_input": args.earnings_json or ("fixture" if args.fixture else "not supplied"),
    }
    json_write(output_dir / "csp_bps_opportunity_audit.json", audit)
    (output_dir / "csp_bps_opportunity_report.md").write_text(markdown_report(audit), encoding="utf-8")
    summary = {
        "ok": True,
        "as_of": audit["as_of"],
        "underlyings_evaluated": audit["counts"]["underlyings_evaluated"],
        "csp_eligible": audit["counts"]["csp_eligible"],
        "bps_eligible": audit["counts"]["bps_eligible"],
        "ranked_top20": audit["counts"]["ranked_top20"],
        "provider_failures": len(provider_failures),
        "output_dir": str(output_dir),
    }
    json_write(output_dir / "csp_bps_run_summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 1 if args.exit_nonzero_on_provider_errors and provider_failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RunnerError, ScreeningError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
