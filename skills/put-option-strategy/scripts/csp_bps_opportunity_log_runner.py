#!/usr/bin/env python3
"""Persist CSP/BPS research boards and mark same-day hypothetical outcomes.

This program does not create orders. It only writes research records and conservative
same-day mark-to-exit outcomes to public."csp-bps-opportunity".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


TABLE = "csp-bps-opportunity"


class TrackingError(RuntimeError):
    pass


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rest_base(url: str) -> str:
    value = url.rstrip("/")
    return value if value.endswith("/rest/v1") else f"{value}/rest/v1"


def request_json(base: str, key: str, method: str, path: str, payload: Any | None = None) -> Any:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "resolution=merge-duplicates,return=representation" if "on_conflict=" in path else "return=representation"
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:800]
        raise TrackingError(f"{method} {path} HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TrackingError(f"{method} {path} failed: {type(exc).__name__}") from exc


def source_context(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context = candidate.get("source_context")
    if not isinstance(context, dict):
        return {"jlaw": {}, "quant": {}, "gex": {}, "earnings": {}}
    return {
        "jlaw": context.get("jlaw") if isinstance(context.get("jlaw"), dict) else {},
        "quant": context.get("quant") if isinstance(context.get("quant"), dict) else {},
        "gex": context.get("gex") if isinstance(context.get("gex"), dict) else {},
        "earnings": context.get("earnings") if isinstance(context.get("earnings"), dict) else {},
    }


def option_symbol(contract: dict[str, Any] | None) -> str | None:
    if not isinstance(contract, dict):
        return None
    value = str(contract.get("option_symbol") or "").strip()
    return value or None


def candidate_key(report_date: str, candidate: dict[str, Any]) -> str:
    short = option_symbol(candidate.get("short_put")) or "UNKNOWN_SHORT"
    long = option_symbol(candidate.get("long_put")) or ""
    return ":".join((report_date, str(candidate.get("strategy") or "UNKNOWN"), str(candidate.get("symbol") or "UNKNOWN"), str(candidate.get("expiry") or "UNKNOWN"), short, long or "SINGLE"))


def quote_time(contract: dict[str, Any] | None) -> str | None:
    if not isinstance(contract, dict):
        return None
    value = contract.get("last_tape_time")
    return str(value) if value not in (None, "") else None


def eligible_log_row(candidate: dict[str, Any], report_date: str, run_id: str, report: str, audit: dict[str, Any], rank_in_strategy: int, top_rank: int) -> dict[str, Any]:
    context = source_context(candidate)
    jlaw, quant, gex, earnings = context["jlaw"], context["quant"], context["gex"], context["earnings"]
    short = candidate.get("short_put") if isinstance(candidate.get("short_put"), dict) else {}
    long = candidate.get("long_put") if isinstance(candidate.get("long_put"), dict) else {}
    strategy = str(candidate.get("strategy") or "")
    credit = number(candidate.get("credit"))
    return {
        "opportunity_key": candidate_key(report_date, candidate),
        "run_id": run_id,
        "report_date": report_date,
        "candidate_status": "ELIGIBLE",
        "symbol": str(candidate.get("symbol") or "").upper() or None,
        "exchange": candidate.get("exchange"),
        "strategy": strategy,
        "rank_in_strategy": rank_in_strategy,
        "expiry": candidate.get("expiry"),
        "dte": candidate.get("dte"),
        "short_option_symbol": option_symbol(short),
        "short_strike": number(short.get("strike")),
        "short_delta": number(short.get("delta")),
        "long_option_symbol": option_symbol(long),
        "long_strike": number(long.get("strike")),
        "spread_width": number(candidate.get("width")),
        "spot_price": number(candidate.get("spot")),
        "technical_safety_floor": number(candidate.get("safety_floor")),
        "safety_floor_source": candidate.get("safety_floor_source"),
        "jlaw_run_date": jlaw.get("run_date"),
        "jlaw_classification": jlaw.get("classification"),
        "jlaw_stop_loss": number(jlaw.get("stop_loss")),
        "quant_as_of": quant.get("as_of"),
        "quant_rank": quant.get("rank"),
        "quant_active_rules": quant.get("n_active_rules"),
        "iv_rank": number(jlaw.get("option_iv_rank")),
        "gex_snapshot_ts": gex.get("snapshot_ts"),
        "gex_regime": gex.get("regime"),
        "gex_put_wall": number(gex.get("put_wall")),
        "gex_put_wall_gex_mm": number(gex.get("put_wall_gex_mm")),
        "earnings_date": candidate.get("earnings_date") or earnings.get("date") or earnings.get("report_date"),
        "quote_source": "Unusual Whales",
        "quote_as_of": quote_time(short),
        "quote_status": "VALID_NBBO_AT_SCREEN",
        "short_bid": number(short.get("bid")),
        "short_ask": number(short.get("ask")),
        "long_bid": number(long.get("bid")),
        "long_ask": number(long.get("ask")),
        "entry_credit": credit,
        "collateral": number(candidate.get("collateral")),
        "max_loss": number(candidate.get("max_loss")),
        "return_metric": number(candidate.get("reward_metric")),
        "break_even": number(candidate.get("break_even")),
        "alignment_score": candidate.get("alignment_score"),
        "reason": candidate.get("reason"),
        "caution": candidate.get("caution"),
        "audit_json": {"candidate": candidate, "audit_config": audit.get("config"), "audit_as_of": audit.get("as_of"), "top20_rank": top_rank},
        "report_markdown": report,
        "eod_outcome": "PENDING",
    }


def no_setup_row(report_date: str, run_id: str, report: str, audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "opportunity_key": f"{report_date}:NO_QUALIFIED_SETUP:{run_id}",
        "run_id": run_id,
        "report_date": report_date,
        "candidate_status": "NO_QUALIFIED_SETUP",
        "quote_source": "Unusual Whales",
        "quote_status": "NO_QUALIFIED_SETUP",
        "reason": "No CSP or BPS candidate passed all configured non-score hard gates.",
        "caution": "Score is a ranking field only. No mechanically eligible structure exists; this record is excluded from the EOD win-rate denominator.",
        "audit_json": audit,
        "report_markdown": report,
        "eod_outcome": "NOT_APPLICABLE",
        "eod_outcome_notes": "No qualified opportunity was logged.",
    }


def build_log_rows(audit: dict[str, Any], report: str, run_id: str) -> list[dict[str, Any]]:
    report_date = str(audit.get("as_of") or date.today().isoformat())
    ranked = audit.get("ranked_top20") if isinstance(audit.get("ranked_top20"), list) else []
    # Compatibility fallback for reports written under the pre-v3 threshold policy.
    if not ranked:
        qualified = audit.get("qualified") if isinstance(audit.get("qualified"), dict) else {}
        ranked = [*(qualified.get("CSP") if isinstance(qualified.get("CSP"), list) else []), *(qualified.get("BPS") if isinstance(qualified.get("BPS"), list) else [])]
    rows: list[dict[str, Any]] = []
    ranks: dict[str, int] = {"CSP": 0, "BPS": 0}
    for top_rank, candidate in enumerate(ranked, start=1):
        if isinstance(candidate, dict):
            strategy = str(candidate.get("strategy") or "")
            ranks[strategy] = ranks.get(strategy, 0) + 1
            rows.append(eligible_log_row(candidate, report_date, run_id, report, audit, ranks[strategy], top_rank))
    return rows or [no_setup_row(report_date, run_id, report, audit)]


def upsert_rows(base: str, key: str, rows: list[dict[str, Any]]) -> Any:
    path = f"/{urllib.parse.quote(TABLE, safe='-')}?on_conflict=opportunity_key"
    return request_json(base, key, "POST", path, rows)


def normalize_quotes(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("quotes"), list):
        payload = payload["quotes"]
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}
    if isinstance(payload, list):
        return {str(item.get("opportunity_key")): item for item in payload if isinstance(item, dict) and item.get("opportunity_key")}
    raise TrackingError("Quotes JSON must be an object keyed by opportunity_key or a list of quote objects.")


def eod_update(row: dict[str, Any], quote: dict[str, Any] | None, as_of_date: str) -> dict[str, Any]:
    if not quote:
        return {
            "eod_quote_date": as_of_date,
            "eod_outcome": "QUOTE_UNAVAILABLE",
            "eod_outcome_notes": "No close-to-exit quote supplied for this opportunity.",
        }
    strategy = str(row.get("strategy") or "")
    short_ask = number(quote.get("short_ask"))
    long_bid = number(quote.get("long_bid"))
    entry_credit = number(row.get("entry_credit"))
    if entry_credit is None or short_ask is None or short_ask <= 0:
        return {
            "eod_quote_date": as_of_date,
            "eod_quote_as_of": quote.get("quote_as_of"),
            "eod_outcome": "QUOTE_UNAVAILABLE",
            "eod_outcome_notes": "Required conservative closing quote is absent or invalid.",
        }
    if strategy == "CSP":
        close_debit = short_ask * 100
    elif strategy == "BPS":
        if long_bid is None or long_bid < 0:
            return {
                "eod_quote_date": as_of_date,
                "eod_quote_as_of": quote.get("quote_as_of"),
                "eod_outcome": "QUOTE_UNAVAILABLE",
                "eod_outcome_notes": "BPS requires both short ask and long bid to mark a conservative close debit.",
            }
        close_debit = (short_ask - long_bid) * 100
    else:
        raise TrackingError(f"Unsupported strategy in log row: {strategy!r}")
    pnl = entry_credit - close_debit
    outcome = "WIN" if pnl > 0.005 else "LOSS" if pnl < -0.005 else "BREAKEVEN"
    update = {
        "eod_quote_date": as_of_date,
        "eod_quote_as_of": quote.get("quote_as_of"),
        "eod_short_bid": number(quote.get("short_bid")),
        "eod_short_ask": short_ask,
        "eod_long_bid": long_bid,
        "eod_long_ask": number(quote.get("long_ask")),
        "eod_close_debit": round(close_debit, 4),
        "eod_mark_to_market_pnl": round(pnl, 4),
        "eod_outcome": outcome,
        "eod_outcome_notes": "Hypothetical same-day mark using conservative close-to-exit NBBO; not executed-trade P&L.",
    }
    return update


def update_eod_rows(base: str, key: str, rows: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], as_of_date: str, dry_run: bool) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for row in rows:
        opportunity_key = str(row.get("opportunity_key"))
        update = eod_update(row, quotes.get(opportunity_key), as_of_date)
        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        updates.append({"opportunity_key": opportunity_key, **update})
        if not dry_run:
            path = f"/{urllib.parse.quote(TABLE, safe='-')}?opportunity_key=eq.{urllib.parse.quote(opportunity_key, safe='')}"
            request_json(base, key, "PATCH", path, update)
    return updates


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Log CSP/BPS research and mark same-day hypothetical outcomes.")
    sub = root.add_subparsers(dest="command", required=True)
    log = sub.add_parser("log", help="Upsert an audit and Markdown report into Supabase.")
    log.add_argument("--audit", required=True)
    log.add_argument("--report", required=True)
    log.add_argument("--run-id", help="UUID. Uses audit run_id or creates a UUID when omitted.")
    log.add_argument("--dry-run", action="store_true")
    eod = sub.add_parser("mark-eod", help="Update qualified rows with supplied close-to-exit NBBO quotes.")
    eod.add_argument("--quotes-json", required=True, help="Object keyed by opportunity_key or list containing opportunity_key.")
    eod.add_argument("--report-date", default=date.today().isoformat())
    eod.add_argument("--eod-date", default=date.today().isoformat())
    eod.add_argument("--dry-run", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise TrackingError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) are required.")
    base = rest_base(url)
    if args.command == "log":
        audit = json.loads(Path(args.audit).read_text(encoding="utf-8"))
        if not isinstance(audit, dict):
            raise TrackingError("Audit must be a JSON object.")
        report = Path(args.report).read_text(encoding="utf-8")
        run_id = args.run_id or str(audit.get("run_id") or uuid.uuid4())
        try:
            uuid.UUID(run_id)
        except ValueError as exc:
            raise TrackingError("run_id must be a UUID.") from exc
        rows = build_log_rows(audit, report, run_id)
        result = rows if args.dry_run else upsert_rows(base, key, rows)
        print(json.dumps({"ok": True, "action": "log", "rows": len(rows), "result": result}, indent=2, default=str))
        return 0
    quotes = normalize_quotes(json.loads(Path(args.quotes_json).read_text(encoding="utf-8")))
    query = f"/{urllib.parse.quote(TABLE, safe='-')}?report_date=eq.{args.report_date}&candidate_status=in.(ELIGIBLE,QUALIFIED)&select=*"
    rows = request_json(base, key, "GET", query)
    if not isinstance(rows, list):
        raise TrackingError("Opportunity query did not return a list.")
    updates = update_eod_rows(base, key, rows, quotes, args.eod_date, args.dry_run)
    counts: dict[str, int] = {}
    for update in updates:
        outcome = str(update.get("eod_outcome"))
        counts[outcome] = counts.get(outcome, 0) + 1
    print(json.dumps({"ok": True, "action": "mark-eod", "rows": len(updates), "outcomes": counts, "updates": updates}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TrackingError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
