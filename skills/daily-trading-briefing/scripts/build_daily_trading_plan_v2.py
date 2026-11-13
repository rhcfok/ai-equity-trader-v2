#!/usr/bin/env python3
"""Build a local, read-only Daily Trading Plan V2 review report.

All Supabase retrievals are GET requests. The builder never writes Supabase,
updates Notion, opens an order, or treats historical ledger marks as live quotes.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

HKT = timezone(timedelta(hours=8))
US_EXCHANGE_TOKENS = ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "CBOE", "IEX")
NEUTRAL_CSP_UNIVERSE = "core + satellite + watch1 + watch2 + watch3"


def esc(value: Any) -> str:
    return "—" if value is None or value == "" else html.escape(str(value), quote=False)


def n(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def num(value: Any, decimals: int = 2) -> str:
    return f"{n(value):,.{decimals}f}" if n(value) is not None else esc(value)


def pct(value: Any, decimals: int = 1) -> str:
    return f"{n(value) * 100:.{decimals}f}%" if n(value) is not None else esc(value)


def pct_point(value: Any, decimals: int = 1) -> str:
    return f"{n(value):.{decimals}f}%" if n(value) is not None else esc(value)


def mm(value: Any, decimals: int = 1) -> str:
    converted = n(value)
    if converted is None:
        return "—"
    if abs(converted) >= 1_000:
        return f"{converted / 1_000:+,.2f}B"
    return f"{converted:+,.{decimals}f}M"


def usd(value: Any, decimals: int = 0) -> str:
    return f"${n(value):,.{decimals}f}" if n(value) is not None else "—"


def per_share(value: Any) -> str:
    return f"${n(value):,.2f}/sh (${n(value) * 100:,.0f}/contract)" if n(value) is not None else "—"


def tr(values: list[Any]) -> str:
    return "<tr>" + "".join(f"<td>{esc(v)}</td>" for v in values) + "</tr>"


def shorten(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def day(value: Any) -> str:
    return str(value)[:10] if value else "—"


def base() -> str:
    value = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not value:
        raise RuntimeError("SUPABASE_URL is required")
    return value if value.endswith("/rest/v1") else value + "/rest/v1"


def headers() -> dict[str, str]:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY is required")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def get(table: str, **params: str) -> list[dict[str, Any]]:
    response = requests.get(f"{base()}/{quote(table, safe='-_')}", headers=headers(), params=params, timeout=45)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected {table} response: {type(data).__name__}")
    return data


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iso_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def is_us_exchange(value: Any) -> bool:
    exchange = str(value or "").upper()
    return any(token in exchange for token in US_EXCHANGE_TOKENS)


STLDC_ROSTER_STAGES = ("BULL-ALERT", "BULL-CONFIRM", "BEAR-ALERT", "BEAR-CONFIRM")


def stldc_stage_roster(rows: list[dict[str, Any]], watch_by: dict[str, dict[str, Any]], as_of: str) -> tuple[dict[str, list[str]], list[str]]:
    """List the full verified US universe, not only Tier A/B; never infer stages."""
    stages_by: dict[str, set[str]] = defaultdict(set)
    excluded: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        if day(row.get("run_date")) != as_of or not is_us_exchange(watch_by.get(symbol, {}).get("exchange")):
            excluded.add(symbol)
            continue
        stage = str(row.get("stage") or "").strip().upper().replace("_", "-")
        stages_by[symbol].add(stage)
    groups: dict[str, list[str]] = {stage: [] for stage in STLDC_ROSTER_STAGES}
    for symbol, stages in sorted(stages_by.items()):
        if symbol in excluded or len(stages) != 1 or next(iter(stages)) not in groups:
            excluded.add(symbol)
            continue
        groups[next(iter(stages))].append(symbol)
    return groups, sorted(excluded)


def render_stldc_roster(groups: dict[str, list[str]], excluded: list[str], as_of: str) -> str:
    lines = [
        f"<b>STLDC 分组名单 | Stage ticker lists:</b> source trade-stldc-v2; as-of {esc(as_of)}; full verified U.S.-exchange universe, not just Tier A/B.",
        '<table fit-page-width="true" header-row="true">',
    ]
    for cells in (["STLDC 分组 / Stage", "数量 / Count", "全部标的 / All tickers"] , *[
        [stage, len(groups[stage]), ", ".join(groups[stage]) or "None / 无"] for stage in STLDC_ROSTER_STAGES
    ]):
        lines.extend(["<tr>", *[f"<td>{esc(cell)}</td>" for cell in cells], "</tr>"])
    lines.append("</table>")
    lines.append("STLDC alert/confirm labels describe technical stages only; they are not option-flow verdicts, reconciliation confirmations, or approved trades. 技术 alert/confirm 不等同期权流或三重确认，不构成开仓批准。")
    if excluded:
        lines.append(f"Scope/QA exclusions: {len(excluded)} symbols have an unverified/non-U.S. exchange, a missing/different source date, or ambiguous/unrecognized stage; omitted, not reclassified.")
    return "\n".join(lines)


def option_legs(row: dict[str, Any]) -> str:
    short = row.get("short_option_symbol")
    long = row.get("long_option_symbol") or row.get("leg2_option_symbol")
    if short:
        return f"short {short}" + (f"; long {long}" if long else "")
    short_strike = row.get("short_strike")
    long_strike = row.get("long_strike") or row.get("leg2_strike")
    kind = row.get("short_type") or ("P" if any(x in str(row.get("strategy")) for x in ("PUT", "CSP")) else "")
    if short_strike:
        return f"short {short_strike}{kind}" + (f"; long {long_strike}{kind}" if long_strike else "")
    return "no contract (status record)"


def earnings_text(flow: dict[str, Any], report_day: date) -> tuple[str, bool]:
    earnings = iso_day(flow.get("next_earnings_date"))
    if flow.get("earnings_block"):
        return f"{esc(flow.get('next_earnings_date'))}; block", False
    if not earnings:
        return "not populated; verify", False
    if earnings < report_day:
        return f"{earnings}; past-date anomaly—exclude", False
    return f"{earnings}; {'safe as stored' if earnings >= report_day + timedelta(days=14) else 'within 14d—exclude'}", earnings >= report_day + timedelta(days=14)


def detail_stldc(row: dict[str, Any] | None) -> str:
    if not row:
        return "no US-scope row"
    return f"{row.get('stage')}; {row.get('category')}; ST {row.get('st_dir')} / LDC {row.get('ldc_signal')}"


def detail_dp(row: dict[str, Any] | None) -> str:
    if not row:
        return "no row"
    state = "accumulation" if row.get("accumulating") is True else "distribution" if row.get("accumulating") is False else "unclassified"
    return f"{mm((n(row.get('net_premium')) or 0) / 1e6)}; {state}; {row.get('print_count') or 0} prints"


def detail_unusual(row: dict[str, Any] | None, as_of: str) -> str:
    if not row:
        return f"no normalized {as_of} row; no incremental confirmation"
    return (f"{row.get('net_option_flow_bias') or '—'}; C/P {row.get('unusual_call_count') or 0}/{row.get('unusual_put_count') or 0}; "
            f"unusual {usd(row.get('unusual_premium'))}; net-z {n(row.get('net_bias_z')) or 0:+.2f}; "
            f"{shorten(row.get('option_flow_summary'), 95)}")


def parse_authors(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return {str(k): str(v) for k, v in result.items()} if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build(args: argparse.Namespace) -> tuple[Path, Path]:
    report_day = date.fromisoformat(args.date)
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    regime, reconcile = load(args.regime_json), load(args.reconcile_json)

    jlaw = get("trade-jlaw-v2", select="run_date,symbol,jlaw_review_type,classification,jlaw_score,current_price,entry_price,stop_loss,target_price,rrr", run_date=f"eq.{args.date}", order="symbol.asc,jlaw_review_type.asc", limit="1000")
    flows = get("flow_verdict_rows", select="verdict_date,symbol,verdict,alert_count_total,earnings_block,next_earnings_date,key_flows,updated_at", verdict_date=f"eq.{args.date}", order="symbol.asc", limit="1000")
    handoff = get("flow-verdicts", select="verdict_date,status,rated_count,candidates_total,updated_at,payload", verdict_date=f"eq.{args.date}", limit="1")
    st_date = str(regime["pillars"]["breadth"]["detail"].get("stldc_run_date") or args.date)
    stldc = []
    offset = 0
    while True:
        page = get("trade-stldc-v2", select="run_date,symbol,stage,category,st_dir,ldc_signal", run_date=f"eq.{st_date}", order="symbol.asc,id.asc", limit="500", offset=str(offset))
        stldc.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    # 7-day stage-transition window (Part 3.0b): newly BULL-ALERT / BULL-CONFIRM
    stldc_hist = []
    h_offset = 0
    h_from = (date.fromisoformat(st_date) - timedelta(days=6)).isoformat()
    while True:
        hpage = get("trade-stldc-v2", select="run_date,symbol,stage", run_date=f"gte.{h_from}", order="run_date.asc,symbol.asc", limit="1000", offset=str(h_offset))
        stldc_hist.extend(hpage)
        if len(hpage) < 1000:
            break
        h_offset += len(hpage)
    stage_series: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _hr in stldc_hist:
        stage_series[str(_hr.get("symbol"))].append((str(_hr.get("run_date")), str(_hr.get("stage") or "")))
    new_bull_alert, new_bull_confirm = [], []
    for _sym, _series in sorted(stage_series.items()):
        if not _series:
            continue
        first_stage, last_stage = _series[0][1], _series[-1][1]
        last_date = _series[-1][0]
        if last_stage == "BULL-ALERT" and first_stage != "BULL-ALERT":
            new_bull_alert.append((_sym, first_stage, last_date))
        elif last_stage == "BULL-CONFIRM" and first_stage != "BULL-CONFIRM":
            new_bull_confirm.append((_sym, first_stage, last_date))
    darkpool = get("darkpool_symbol_daily", select="*", fetch_date=f"eq.{args.date}", order="symbol.asc", limit="1000")
    davis = get("option-opportunity-davis", select="*", report_date=f"eq.{args.date}", order="symbol.asc", limit="100")
    csp_history = get("csp-bps-opportunity", select="*", order="report_date.desc,rank_in_strategy.asc.nullslast,symbol.asc", limit="100")
    uw_history = get("uw-option-opportunities", select="*", order="report_date.desc,confidence_score.desc.nullslast,symbol.asc", limit="100")
    flow_dates = get("option_flow_symbol_signals", select="signal_date", order="signal_date.desc", limit="1")
    unusual_date = str(flow_dates[0].get("signal_date")) if flow_dates else "not available"
    intelligence = get("substack-intelligence", select="*", order="summary_date.desc,created_at.desc", limit="1")

    flows_by = {str(x.get("symbol") or "").upper(): x for x in flows}
    rec_rows = reconcile["reconciliation"]
    rec_by = {str(x.get("symbol") or "").upper(): x for x in rec_rows}
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in jlaw:
        grouped[str(row.get("symbol") or "").upper()][str(row.get("jlaw_review_type") or "").lower()] = row

    preliminary_b, candidate_pool = [], 0
    excluded_conflict = excluded_bear = excluded_earnings = 0
    for symbol, pair in grouped.items():
        chart = pair.get("chart")
        score = n(chart.get("jlaw_score")) if chart else None
        qualifies = chart and (chart.get("classification") == "Valid" or (chart.get("classification") == "Watch" and (score or 0) >= 12))
        if not qualifies:
            continue
        candidate_pool += 1
        flow, rec = flows_by.get(symbol, {}), rec_by.get(symbol, {})
        _, earnings_safe = earnings_text(flow, report_day)
        if rec.get("status") == "conflict":
            excluded_conflict += 1
        elif flow.get("verdict") in {"bearish", "protective", "covered-call-selling"}:
            excluded_bear += 1
        elif not earnings_safe:
            excluded_earnings += 1
        elif chart.get("classification") == "Watch" and (score or 0) >= 12 and flow.get("verdict") == "bullish" and rec.get("status") == "confirmed":
            preliminary_b.append((symbol, chart, flow, rec))

    all_symbols = sorted({x[0] for x in preliminary_b} | {str(r.get("symbol") or "").upper() for r in stldc + davis + csp_history + uw_history if r.get("symbol")})
    watchlist = []
    for start in range(0, len(all_symbols), 100):
        symbols = all_symbols[start:start + 100]
        watchlist.extend(get("watchlist", select="symbol,exchange,category", symbol=f"in.({','.join(symbols)})", order="symbol.asc", limit="1000"))
    watch_by = {str(x.get("symbol") or "").upper(): x for x in watchlist}
    stldc_groups, stldc_exclusions = stldc_stage_roster(stldc, watch_by, st_date)
    stldc_roster_markdown = render_stldc_roster(stldc_groups, stldc_exclusions, st_date)
    in_scope = lambda symbol: is_us_exchange((watch_by.get(symbol) or {}).get("exchange"))
    tier_b = sorted([x for x in preliminary_b if in_scope(x[0])], key=lambda x: (-(n(x[1].get("rrr")) or -999), x[0]))
    b_symbols = [x[0] for x in tier_b]
    unusual = get("option_flow_symbol_signals", select="*", signal_date=f"eq.{unusual_date}", ticker=f"in.({','.join(b_symbols)})", order="ticker.asc", limit="1000") if b_symbols and unusual_date != "not available" else []
    # market-wide normalized flow for Part 3.4 debrief (all tickers, not just Tier B)
    unusual_all = get("option_flow_symbol_signals", select="*", signal_date=f"eq.{unusual_date}", order="net_bias_z.desc", limit="1000") if unusual_date != "not available" else []

    st_by = {str(x.get("symbol") or "").upper(): x for x in stldc}
    dp_by = {str(x.get("symbol") or "").upper(): x for x in darkpool}
    unusual_by = {str(x.get("ticker") or "").upper(): x for x in unusual}
    bull = sum(str(x.get("stage") or "").startswith("BULL") for x in stldc)
    bear = sum(str(x.get("stage") or "").startswith("BEAR") for x in stldc)
    bull_confirm = sum(x.get("stage") == "BULL-CONFIRM" for x in stldc)
    bear_confirm = sum(x.get("stage") == "BEAR-CONFIRM" for x in stldc)
    bull_alert = sum(x.get("stage") == "BULL-ALERT" for x in stldc)
    bear_alert = sum(x.get("stage") == "BEAR-ALERT" for x in stldc)
    net_dp = sum(n(x.get("net_premium")) or 0 for x in darkpool) / 1e6
    dp_up = sorted([x for x in darkpool if (n(x.get("net_premium")) or 0) > 0], key=lambda x: n(x.get("net_premium")) or 0, reverse=True)
    dp_down = sorted([x for x in darkpool if (n(x.get("net_premium")) or 0) < 0], key=lambda x: n(x.get("net_premium")) or 0)
    up_text = ", ".join(f"{x.get('symbol')} {mm((n(x.get('net_premium')) or 0)/1e6)}" for x in dp_up[:6]) or "none"
    down_text = ", ".join(f"{x.get('symbol')} {mm((n(x.get('net_premium')) or 0)/1e6)}" for x in dp_down[:5]) or "none"

    chart = [x for x in jlaw if str(x.get("jlaw_review_type")).lower() == "chart"]
    yahoo = [x for x in jlaw if str(x.get("jlaw_review_type")).lower() == "yahoo"]
    chart_valid, yahoo_valid = sum(x.get("classification") == "Valid" for x in chart), sum(x.get("classification") == "Valid" for x in yahoo)
    chart_watch, yahoo_watch = sum(x.get("classification") == "Watch" for x in chart), sum(x.get("classification") == "Watch" for x in yahoo)
    verdicts, statuses = Counter(str(x.get("verdict")) for x in flows), Counter(str(x.get("status")) for x in rec_rows)
    conflicts = sorted([x for x in rec_rows if x.get("status") == "conflict"], key=lambda x: str(x.get("symbol")))

    csp_days = sorted({str(x.get("report_date")) for x in csp_history if x.get("report_date")}, reverse=True)
    csp_day = csp_days[0] if csp_days else "not available"
    csp_latest = [x for x in csp_history if str(x.get("report_date")) == csp_day]
    old_days = sorted({str(x.get("report_date")) for x in csp_history if x.get("candidate_status") == "ELIGIBLE" and str(x.get("report_date")) < csp_day}, reverse=True)
    old_csp_day = old_days[0] if old_days else "not available"
    old_csp = [x for x in csp_history if str(x.get("report_date")) == old_csp_day and x.get("candidate_status") == "ELIGIBLE" and in_scope(str(x.get("symbol") or "").upper())]
    uw_days = sorted({str(x.get("report_date")) for x in uw_history if x.get("report_date")}, reverse=True)
    uw_day = uw_days[0] if uw_days else "not available"
    uw = [x for x in uw_history if str(x.get("report_date")) == uw_day and in_scope(str(x.get("symbol") or "").upper())]
    davis = [x for x in davis if in_scope(str(x.get("symbol") or "").upper())]

    p, tide = regime["pillars"], regime["pillars"]["flow"]["detail"]
    playbook = dict(regime["playbook"])
    # 2026-11-04 instruction: weak regime changes risk size, not income-family permission.
    playbook.update({"naked_csp": NEUTRAL_CSP_UNIVERSE + "; individual checks", "csp_dte_max": 35})
    playbook["income_strategies"] = ["WHEEL_CSP", "WHEEL_CC", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"]
    intelligence_row = intelligence[0] if intelligence else {}
    authors = parse_authors(intelligence_row.get("author_sections"))
    note = Path(args.fragility_notes).read_text(encoding="utf-8") if args.fragility_notes else ""
    sd = regime["pillars"]["stress"].get("detail") or {}
    fragility = (f"LPI {sd.get('lpi_tier', 'n/a')} (dashboard as-of {sd.get('as_of', 'n/a')}); funding {sd.get('funding_legs', 'n/a')} legs; lagged weekly cross-check; source notes: fragility_dashboard_source.md"
                 if sd else ("Dashboard note unavailable; use deterministic regime JSON only."))

    fresh_rows = "".join(tr([x.get("table"), x.get("latest"), x.get("age_weekdays"), "daily" if x.get("max_age") == 0 else "T+1 accepted" if x.get("max_age") == 1 else f"≤{x.get('max_age')} trading days", x.get("status"), "within expected cadence" if x.get("status") == "ok" else "withhold affected conclusion"]) for x in reconcile["freshness"])
    conflict_rows = "".join(tr([x.get("symbol"), x.get("verdict"), f"{num(x.get('premium_today'))} / {num(x.get('premium_3d'))}", num(x.get("net_bias_z")), f"{mm(x.get('dp_net_mm'))} / {args.date}", f"{num(x.get('flow_composite'), 0)} {x.get('flow_label') or '—'}", ", ".join(x.get("flow_conflicts") or []) or "unresolved", "skip / re-run; not clean directional view"]) for x in conflicts)

    b_rows = []
    for symbol, c, f, r in tier_b:
        entry, stop, rrr = n(c.get("entry_price")), n(c.get("stop_loss")), n(c.get("rrr"))
        earnings, _ = earnings_text(f, report_day)
        b_rows.append(tr([symbol, "conditional long buy-stop; review only", num(entry * 1.001) if entry else "—", num(stop), pct((entry - stop)/entry) if entry and stop is not None else "—", num(c.get("target_price")) if n(c.get("target_price")) and n(c.get("target_price")) > (entry or 0) else "—", (num(rrr) + "; skippable" if rrr is not None and rrr < .5 else num(rrr)), earnings, detail_stldc(st_by.get(symbol)), detail_dp(dp_by.get(symbol)), detail_unusual(unusual_by.get(symbol), unusual_date), f"{f.get('verdict')} / {f.get('alert_count_total') or '—'} alerts; {r.get('status')}", "none recorded; refresh required", "0.5x after fresh rerun", f"JLaw {args.date}; STLDC {st_date}; DP {args.date}; normalized {unusual_date}"]))
    if not b_rows:
        b_rows = [tr(["—", "No strict US-listed review candidate"] + ["—"] * 13)]

    b_compare_rows = []
    for symbol, c, f, r in tier_b:
        b_compare_rows.append(tr([
            "Tier B", symbol,
            f"chart {c.get('classification')} {num(c.get('jlaw_score'), 1)}; RRR {num(c.get('rrr'), 2)}",
            detail_stldc(st_by.get(symbol)), detail_dp(dp_by.get(symbol)),
            detail_unusual(unusual_by.get(symbol), unusual_date),
            f"{f.get('verdict')} / reconciliation {r.get('status')}",
            "conditional; 0.5x only after a fresh rerun/trigger",
        ]))
    if not b_compare_rows:
        b_compare_rows = [tr(["Tier B", "none", "—", "—", "—", "—", "—", "no qualifying US-listed review card"])]

    author_rows = []
    for name, key in [("Mimi / James", "mimivsjames2"), ("Street Alpha", "streetalpha"), ("Kova", "kova"), ("J Law", "jlaw")]:
        if authors.get(key):
            author_rows.append(tr([name, shorten(authors[key], 330), f"{intelligence_row.get('edition') or '—'} / {intelligence_row.get('summary_date') or '—'}", "attributed context only; cannot override model controls"]))
    if not author_rows:
        author_rows = [tr(["—", "No author section stored", "—", "—"])]

    davis_rows = []
    for x in sorted(davis, key=lambda a: (-(n(a.get("score")) or -999), str(a.get("symbol")))):
        dp = f"DP {mm(x.get('dp_net_mm'))} {'accum' if x.get('dp_accumulating') else 'distrib' if x.get('dp_accumulating') is False else '—'}"
        stored_loss = n(x.get("max_loss"))
        loss_text = (f"stored max loss {per_share(stored_loss)} [negative-field anomaly; not a usable risk figure]"
                     if stored_loss is not None and stored_loss < 0 else f"max loss {per_share(stored_loss)}")
        economics = f"credit {per_share(x.get('entry_credit'))}; {loss_text}; annualized {pct_point(x.get('annualized_pct'))}"
        quote = f"{day(x.get('quote_as_of'))}; {x.get('quote_status') or 'status —'}"
        davis_rows.append(tr([f"Davis / {day(x.get('report_date'))}", x.get("symbol"), x.get("strategy"), option_legs(x), f"{day(x.get('expiry'))} / stored {x.get('dte')}d", economics, quote, f"{x.get('verdict')}; {dp}", f"score {num(x.get('score'),1)} / {x.get('option_tier')}; {shorten(x.get('caution'),120) or 'none'}", "historical 35D mark; no current quote/validation"]))

    davis_negative_loss_count = sum((n(x.get("max_loss")) or 0) < 0 for x in davis)

    csp_rows = []
    for x in csp_latest:
        if x.get("candidate_status") == "NO_QUALIFIED_SETUP":
            csp_rows.append(tr([f"CSP/BPS / {csp_day}", "—", "NO_QUALIFIED_SETUP", "no contract", "—", "—", day(x.get("quote_as_of")), "—", shorten(x.get("reason"),170), "control record; no eligible latest structure"]))
    for x in sorted(old_csp, key=lambda a: (str(a.get("strategy")), n(a.get("rank_in_strategy")) or 999)):
        economics = f"credit {usd(x.get('entry_credit'))}/contract; return metric {pct(x.get('return_metric'))}; max loss {usd(x.get('max_loss'))}"
        csp_rows.append(tr([f"CSP/BPS / {old_csp_day}", x.get("symbol"), x.get("strategy"), option_legs(x), f"{day(x.get('expiry'))} / stored {x.get('dte')}d", economics, day(x.get("quote_as_of")), f"{x.get('exchange')}; GEX {x.get('gex_regime')}; alignment {num(x.get('alignment_score'),1)}", shorten(x.get('caution'),120) or 'none', "archived US-exchange record; current NBBO/event gates required"]))

    uw_rows = []
    for x in sorted(uw, key=lambda a: (-(n(a.get("confidence_score")) or -999), str(a.get("symbol")), str(a.get("strategy")))):
        economics = f"credit {per_share(x.get('entry_credit'))}; ROR {pct(x.get('return_on_risk'))}; annualized {pct_point(x.get('annualized_pct'))}"
        disposition = "historical only; share ownership + current chain required" if x.get("strategy") == "WHEEL_CC" else "historical only; fresh spot/NBBO/OI/earnings required"
        uw_rows.append(tr([f"UW income / {uw_day}", x.get("symbol"), x.get("strategy"), option_legs(x), f"{day(x.get('short_expiry') or x.get('expiry'))} / stored {x.get('short_dte') or x.get('dte')}d", economics, day(x.get("quote_as_of")), f"{x.get('verdict')}; {x.get('flow_label')}; DP {mm(x.get('dp_net_mm'))}", f"{x.get('score_band')} {num(x.get('confidence_score'),1)}; {shorten(x.get('caution'),120) or 'none'}", disposition]))

    # Part 3.0b: stage-transition highlight lists
    trans_rows = "".join(tr([s, f"{st_date} → {d}", f"{prev} → BULL-ALERT", "7d window"]) for s, prev, d in new_bull_alert)
    trans_rows += "".join(tr([s, f"{st_date} → {d}", f"{prev} → BULL-CONFIRM", "7d window"]) for s, prev, d in new_bull_confirm)
    # Part 3.4: dark-pool + market-wide unusual flow debrief inputs
    dp_sorted = sorted(darkpool, key=lambda x: (n(x.get("net_premium")) or 0), reverse=True)
    dp_pos = [x for x in dp_sorted if (n(x.get("net_premium")) or 0) > 0][:8]
    dp_neg = sorted([x for x in dp_sorted if (n(x.get("net_premium")) or 0) < 0], key=lambda x: (n(x.get("net_premium")) or 0))[:8]
    dp_rows = "".join(tr([x.get("symbol"), mm((n(x.get("net_premium")) or 0) / 1e6), pct((n(x.get("buy_premium")) or 0) / max(n(x.get("total_premium")) or 1, 1e-9), 1), x.get("print_count"), x.get("block_count"), str(x.get("accumulating"))]) for x in dp_pos)
    dp_rows += "".join(tr([x.get("symbol"), mm((n(x.get("net_premium")) or 0) / 1e6), pct((n(x.get("buy_premium")) or 0) / max(n(x.get("total_premium")) or 1, 1e-9), 1), x.get("print_count"), x.get("block_count"), str(x.get("accumulating"))]) for x in dp_neg)
    uf_top = sorted(unusual_all, key=lambda x: abs(n(x.get("net_bias_z")) or 0), reverse=True)[:8]
    uf_rows = "".join(tr([x.get("ticker"), num(x.get("net_bias_z"), 2), mm((n(x.get("net_directional_premium")) or 0) / 1e6), f"{n(x.get('bullish_pct')) or 0:.0f}%", f"{n(x.get('bearish_pct')) or 0:.0f}%", (x.get("dominant_structure") or "—"), (x.get("directional_confidence") or "—")]) for x in uf_top)
    dp_total = sum(n(x.get("net_premium")) or 0 for x in darkpool) / 1e6
    source_snapshot = {"retrieved_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "report_date": args.date, "mode": "local_review_only_no_external_writes", "policy_override": {"naked_csp": playbook["naked_csp"], "csp_dte_max": playbook["csp_dte_max"], "note": "2026-11-01 user instruction supersedes prior neutral core-only and shorter-duration text."}, "regime": regime, "reconciliation": reconcile, "jlaw_rows": jlaw, "flow_verdict_rows": flows, "flow_handoff": handoff, "stldc_rows": stldc, "stldc_hist_rows": stldc_hist, "darkpool_rows": darkpool, "normalized_unusual_rows": unusual, "normalized_unusual_market_rows": unusual_all, "watchlist_us_scope": watchlist, "option_davis_rows": davis, "csp_bps_rows": csp_history, "uw_income_rows": uw_history, "intelligence": intelligence, "income_board_markdown": str(Path(args.income_board_markdown).resolve()) if args.income_board_markdown else None}
    source_snapshot["stldc_stage_roster"] = {"as_of": st_date, "groups": stldc_groups, "excluded_symbols": stldc_exclusions, "scope": "verified U.S.-exchange universe"}
    snapshot = output / f"daily_trading_plan_v2_{args.date}_source_snapshot.json"
    source_snapshot["policy_override"]["note"] = "2026-11-04 user instruction: no blanket weak-regime CSP/SP block; CSP<=35d; soft6/4/10 EM-ranked board. Deterministic regime math unchanged."
    source_snapshot["income_board_manifest"] = getattr(args, "income_board_manifest", None)
    snapshot.write_text(json.dumps(source_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tide_state = "defensive" if (n(tide.get("tide_latest_mm")) or 0) < 0 else "constructive" if (n(tide.get("tide_latest_mm")) or 0) > 0 else "neutral"
    b_confirm = sum(st_by.get(s, {}).get("stage") == "BULL-CONFIRM" for s in b_symbols)
    b_accum = sum(dp_by.get(s, {}).get("accumulating") is True for s in b_symbols)
    b_unusual_directional = sum(str(unusual_by.get(s, {}).get("net_option_flow_bias")) not in {"", "None", "insufficient_evidence"} for s in b_symbols)
    report = f'''<toggle heading="h1">
{args.date} · 📊 交易计划 V2 — 扩展审阅版 | Daily Trading Plan V2 — Expanded Review

<callout icon="⚠️" color="yellow_bg">
<b>状态 Status:</b> REVIEW ONLY / MARKET CLOSED / ARCHIVE-READY · 市场会话: {args.date} U.S. close · 生成: {datetime.now(HKT).strftime('%Y-%m-%d %H:%M HKT')}.<br />
本文件仅供审阅，不会创建、传送、修改或提交订单，也不会写入源数据库。若发布至 Notion，仅为归档，不构成交易动作。历史期权记录是带日期的来源摘录，不是当前报价或订单建议。<br />
Research review only. Independently re-check current price, NBBO, liquidity, contract terms, event risk, fees, and suitability before any action.
</callout>

## Part 1 · 市场状态 | Market Regime

<b>有效档位:</b> {esc(regime['band'])} · <b>原始档位:</b> {esc(regime['raw_band'])} · <b>得分:</b> {num(regime['score'],1)}/100 · <b>迟滞:</b> {esc(regime['hysteresis'])}.

### 1.1 Regime scorecard | Regime 评分卡
<table fit-page-width="true" header-row="true">
<tr><th>支柱</th><th>权重</th><th>得分</th><th>证据</th><th>来源 / as-of</th><th>状态</th></tr>
{tr(['趋势 Trend','30%',num(p['trend']['score'],1),f"4/8 MA checks; Markov {p['trend']['detail']['markov']['state']}, next BEAR {p['trend']['detail']['markov']['p_next']['BEAR']:.3f}",f'regime-score / {args.date}','completed'])}
{tr(['波动率 Volatility','25%',num(p['vol']['score'],1),f"VIX {p['vol']['detail']['vix']}; VIX/VIX3M {p['vol']['detail']['vix_vix3m']}; RV20 {p['vol']['detail']['spy_rv20']}",f'regime-score / {args.date}','completed'])}
{tr(['广度 Breadth','20%',num(p['breadth']['score'],1),f"STLDC BULL {bull} / BEAR {bear}; confirms {bull_confirm}/{bear_confirm}; chart Valid {p['breadth']['detail']['valid_count']}",f'STLDC {st_date}; JLaw {args.date}','T+1 accepted / completed'])}
{tr(['期权流 Flow','15%',num(p['flow']['score'],1),f"5d avg {tide['tide_5d_avg_mm']:+.1f}M; positive {tide['tide_pos_sessions']}; latest {tide['tide_latest_mm']:+.1f}M",f"market tide {tide['tide_latest_date']}",'T+1 accepted'])}
{tr(['脆弱度 Stress','10%',num(p['stress']['score'],1),f"LPI {p['stress']['detail'].get('lpi_tier','n/a')}; funding {p['stress']['detail'].get('funding_legs','n/a')}",f"Fragility dashboard / {p['stress']['detail'].get('as_of','n/a')}",'lagged cross-check'])}
</table>

### 1.2 Regime inputs | 市场结构输入
- <b>Market tide / 市场潮汐:</b> {tide['tide_latest_date']} was {tide_state}: all 39 afternoon buckets negative, averaging {mm(tide['tide_latest_mm'])} net call premium per 5-minute bucket. The five-session window is {mm(tide['tide_5d_avg_mm'])}, with {tide['tide_pos_sessions']} positive sessions.
- <b>Chart / Yahoo:</b> chart {len(chart)} rows ({chart_valid} Valid / {chart_watch} Watch); Yahoo {len(yahoo)} rows ({yahoo_valid} Valid / {yahoo_watch} Watch), both {args.date}.
- <b>Flow coverage:</b> {len(flows)}/{len(flows)} rated: bullish {verdicts.get('bullish',0)}, mixed {verdicts.get('mixed',0)}, bearish {verdicts.get('bearish',0)}, protective {verdicts.get('protective',0)}, no-data {verdicts.get('no-data',0)}.
- <b>STLDC / 宽度:</b> {st_date}, {len(stldc)} US-listed watchlist names: <b>BEAR {bear} vs BULL {bull} ({bear/bull:.1f}:1)</b>; BEAR-CONFIRM {bear_confirm} / BEAR-ALERT {bear_alert}, BULL-CONFIRM {bull_confirm} / BULL-ALERT {bull_alert}. SPY regime {p['breadth']['detail']['spy_regime_stldc']}.
- <b>Dark pool / 暗池:</b> {args.date} board, {len(darkpool)} symbols, nets <b>{mm(net_dp)}</b>. Accumulation: {up_text}. Distribution: {down_text}.
- <b>解读 Read:</b> Tide is {tide_state} while dark pool shows {mm(net_dp)} net {'accumulation' if net_dp > 0 else 'distribution'} and STLDC is near parity. This two-source divergence does not override the {esc(regime['band'])} band: Tier B remains a conditional, half-sized review card after a fresh rerun.
- <b>Fragility dashboard:</b> {fragility}

### 1.3 背离、解读与失效条件 | Divergences, Read-through & Invalidation
Trend ({num(p['trend']['score'],1)}) and breadth ({num(p['breadth']['score'],1)}) are the chief drags. The flow five-day window ({num(p['flow']['score'],1)}) and stress ({num(p['stress']['score'],1)}) are comparatively supportive. The latest defensive tide and {mm(net_dp)} dark-pool accumulation are disclosed divergence, not an unmodelled override.

<b>System-wide invalidation:</b> if a fresh completed-session run produces a lower effective regime, move every candidate down one tier and rebuild the review board. Historical levels expire after the next market session.

### 执行规则 | Playbook
<table fit-page-width="true" header-row="true">
<tr><th>Tier A</th><th>Tier B</th><th>允许策略</th><th>裸 CSP</th><th>CSP DTE cap</th><th>信用风险</th><th>财报处理</th><th>降档行动</th></tr>
{tr([f"{playbook['size_mult']}x after fresh validation",f"{playbook['tier_b_size_mult']}x after fresh trigger",', '.join(playbook['income_strategies']),playbook['naked_csp'],f"≤{playbook['csp_dte_max']} calendar days",f"{playbook['credit_at_risk_pct_nlv']:.2f}% NLV",'no new entry inside 14-day stored block; re-check calendar','move each candidate down one tier and rebuild'])}
</table>
<b>Policy amendment:</b> for this {esc(regime['band'])} review, naked CSP scope is <b>{NEUTRAL_CSP_UNIVERSE}</b> and the CSP DTE cap is <b>35 calendar days</b>. This supersedes earlier shorter-duration policy text.

---
## Part 2 · 情报、对账与新鲜度 | Intelligence, Reconciliation & Freshness

### 2.0 情报要点 | Intelligence highlights
- <b>Vintage:</b> {esc(intelligence_row.get('edition'))}, dated {esc(intelligence_row.get('summary_date'))}; user-connected third-party context, not a price or order source.
- <b>Core narrative:</b> {esc(intelligence_row.get('headline') or 'No intelligence summary retrieved.')}
- <b>Author-led lenses / 作者观点:</b> author attribution is explicit below, not blended into systematic evidence.
<table fit-page-width="true" header-row="true"><tr><th>作者</th><th>核心叙事 / attributed message</th><th>版本</th><th>使用边界</th></tr>{''.join(author_rows)}</table>
- <b>Boundary:</b> author views cannot override regime, earnings, reconciliation, dark-pool, STLDC, unusual-flow, or risk controls.

### 2.1 数据新鲜度 | Data freshness
<table fit-page-width="true" header-row="true"><tr><th>数据集</th><th>最新日期</th><th>滞后</th><th>节奏</th><th>状态</th><th>影响</th></tr>{fresh_rows}</table>

### 2.2 对账总览 | Reconciliation overview
<table fit-page-width="true" header-row="true"><tr><th>Universe</th><th>Confirmed</th><th>Conflict</th><th>Partial</th><th>Confirmed bullish</th><th>Confirmed bearish</th><th>No-data/protective</th><th>Frozen snapshot</th></tr>{tr([len(rec_rows),statuses.get('confirmed',0),statuses.get('conflict',0),statuses.get('partial',0),sum(x.get('status')=='confirmed' and x.get('verdict')=='bullish' for x in rec_rows),sum(x.get('status')=='confirmed' and x.get('verdict')=='bearish' for x in rec_rows),verdicts.get('no-data',0)+verdicts.get('protective',0),f'flow_reconcile_{args.date}.json'])}</table>

### 2.3 冲突登记册 | Conflict register
<table fit-page-width="true" header-row="true"><tr><th>Ticker</th><th>Agent verdict</th><th>Premium today / 3d</th><th>net_bias_z</th><th>Dark pool</th><th>Composite</th><th>Conflict</th><th>Action</th></tr>{conflict_rows}</table>

### 2.4 交叉证据 | Cross-evidence
<table fit-page-width="true" header-row="true">
<tr><th>范围</th><th>来源</th><th>时点</th><th>观察</th><th>一致性</th><th>影响</th></tr>
{tr(['Tier B four-way overlay','JLaw + STLDC + dark pool + normalized unusual flow',f'JLaw {args.date}; STLDC {st_date}; DP {args.date}; normalized {unusual_date}',f'{len(tier_b)} US-listed B candidates; {b_confirm} BULL-CONFIRM; {b_accum} DP accumulation','unusual-flow absence/insufficiency is non-confirmatory','review cards only until fresh rerun'])}
{tr(['Option contracts','Davis + CSP/BPS + UW ledgers',f'Davis {args.date}; CSP/BPS {csp_day}; UW {uw_day}',f'{len(davis)} Davis / {len(csp_latest)} latest CSP/BPS / {len(uw)} UW US-scope rows','all are historical marks','no opening proposal'])}
</table>

---
## Part 3 · 方向性交易计划 | Directional Tier Plan

### 3.0 A/B 级摘要 | Tier summary
<b>Active playbook:</b> {esc(regime['band'])}: A {playbook['size_mult']}x and B {playbook['tier_b_size_mult']}x only after a fresh rerun and trigger.<br />
<b>Tier A:</b> 0; chart Valid was {chart_valid}.<br />
<b>Tier B:</b> {len(tier_b)} strict US-listed historical signal cards. {candidate_pool-len(tier_b)} high-score candidates were withheld by conflict, opposing flow, earnings, non-US scope, or other gates.

### 3.0a 多源比较 | Tier versus STLDC, Dark Pool & Unusual Flow

{stldc_roster_markdown}

<table fit-page-width="true" header-row="true">
<tr><th>层级</th><th>标的</th><th>J Law</th><th>STLDC</th><th>Dark pool</th><th>Unusual flow</th><th>Option-flow verdict / reconciliation</th><th>结论</th></tr>
{tr(['Tier A','none',f'chart Valid {chart_valid}',f'market BULL {bull} / BEAR {bear}',f'market net {mm(net_dp)}',f'latest normalized flow {unusual_date}',f'bullish {verdicts.get("bullish",0)} / bearish {verdicts.get("bearish",0)}','gate not met'])}
{''.join(b_compare_rows)}
</table>
<b>Interpretation:</b> no normalized row and insufficient evidence are not bullish unusual-flow signals; they are explicitly non-confirmatory rather than silently upgraded.

### 3.0b 近 7 日新晋多头阶段 | Newly BULL-ALERT / BULL-CONFIRM (last 7 days)
观察窗 {h_from} → {st_date}（trade-stldc-v2 全量对比）。新晋 = 窗口首日阶段非目标阶段、末日为目标阶段。
<table fit-page-width="true" header-row="true">
<tr><th>BULL-ALERT 新晋</th><th>数量 Count</th><th>标的（前段 → 现阶段，变更日）</th></tr>
{tr(['BULL-ALERT', str(len(new_bull_alert)), '、'.join(f"{s}（{p}→ALERT，{d}）" for s, p, d in new_bull_alert) or 'None / 无'])}
{tr(['BULL-CONFIRM', str(len(new_bull_confirm)), '、'.join(f"{s}（{p}→CONFIRM，{d}）" for s, p, d in new_bull_confirm) or 'None / 无'])}
</table>
分级提示：STLDC 实测 edges 薄（BULL-CONFIRM chained 56.9% vs base 54.3%，grading window 2026-10→11），新晋名单为关注线索而非入场批准；fresh confirm（57.1%）优于 stale（53.5%）。

### 3.1 A 级状态 | Tier A status
Tier A is empty because chart Valid was zero on {args.date}. This is already reflected in the breadth score and is not a second independent downgrade.

### 3.2 B 级执行板 | Tier B execution board
<table fit-page-width="true" header-row="true">
<tr><th>Ticker</th><th>Side / order type</th><th>Trigger</th><th>Stop</th><th>Stop %</th><th>First target</th><th>RRR</th><th>Earnings</th><th>STLDC</th><th>Dark pool</th><th>Unusual flow</th><th>Flow verdict / reconciliation</th><th>Conflict action</th><th>Size</th><th>Source / as-of</th></tr>
{''.join(b_rows)}
</table>

### 3.4 异动流与暗池纪要 | Unusual Flow & Dark Pool Debrief
数据源：darkpool_symbol_daily（fetch_date {args.date}，{len(darkpool)} symbols）与 option_flow_symbol_signals（signal_date {unusual_date}，{len(unusual_all)} symbols，全市场）。暗池当日净额合计 {mm(dp_total)}；异动流按 |net_bias_z| 取前 8。

<b>暗池 | Dark pool — 前 8 净流入 / 前 8 净流出</b>
<table fit-page-width="true" header-row="true">
<tr><th>Symbol</th><th>Net premium</th><th>Buy %</th><th>Prints</th><th>Blocks</th><th>Accumulating</th></tr>
{dp_rows}
</table>

<b>异动流 | Unusual flow — |net_bias_z| 前 8（全市场）</b>
<table fit-page-width="true" header-row="true">
<tr><th>Ticker</th><th>net_bias_z</th><th>Net directional premium</th><th>Bullish %</th><th>Bearish %</th><th>Dom. structure</th><th>Confidence</th></tr>
{uf_rows}
</table>
纪要要点：暗池净流入前列为跟随性吸筹线索、净流出前列为派发线索（买卖比例与 block 数佐证强度）；异动流 |net_bias_z|≥1 视为强方向读数。两者的实测胜率本轮尚未独立分级（win-rate grading 覆盖 flow-verdict/jlaw/STLDC/quant2），故仅作交叉证据，不单独构成方向结论。

---
## Part 4 · 期权收入板 | Options Income Plan
<callout icon="📝" color="blue_bg"><b>Income board withheld:</b> no new option-chain run was made in this closed-market review. The extracts below are requested historical ledger records, with original dates/status/cautions, not current proposals.<br />Current spot, NBBO, OI, DTE, liquidity, event, assignment, fee, and suitability checks are required before independent action.</callout>

### 4.0 护栏、置信度与来源盘点 | Guardrails, confidence & source inventory
- <b>Naked CSP:</b> US-exchange symbols in <b>{playbook['naked_csp']}</b>; <b>CSP DTE ≤{playbook['csp_dte_max']} calendar days</b>.
- <b>Other DTE rules:</b> short call/strangle ≤20 days; BPS/BCS <35 days. Stored DTE is historical, not recalculated for this review.
- <b>Confidence:</b> source scores/bands are ordering fields, not probabilities or sizing instructions. Historical “live” status never equals a current quote.
- <b>US-exchange filter:</b> CSP/BPS uses its explicit exchange. Davis/UW rows are included only when mapped to the US-exchange `watchlist` scope.
- <b>Source QA:</b> {davis_negative_loss_count} Davis bear-call-spread source rows carry a negative stored `max_loss` value. Their raw field is preserved and explicitly labelled an anomaly; it is not used as a risk figure.
<table fit-page-width="true" header-row="true"><tr><th>来源</th><th>Latest stored as-of</th><th>行数 / strategies</th><th>审阅结论</th></tr>
{tr(['option-opportunity-davis',args.date,f'{len(davis)} US-scoped: '+', '.join(f'{k}×{v}' for k,v in sorted(Counter(str(x.get("strategy")) for x in davis).items())),'historical 35D marks; not live proposals'])}
{tr(['csp-bps-opportunity',csp_day,f'latest: '+', '.join(f'{k}×{v}' for k,v in sorted(Counter(str(x.get("candidate_status")) for x in csp_latest).items()))+f'; earlier eligible {old_csp_day}: {len(old_csp)}','latest control plus archived eligible rows'])}
{tr(['uw-option-opportunities',uw_day,f'{len(uw)} US-scoped: '+', '.join(f'{k}×{v}' for k,v in sorted(Counter(str(x.get("strategy")) for x in uw).items())),'historical source extract; CC needs share-ownership check'])}
</table>

### 4.1 收入板 | Live income board
<table fit-page-width="true" header-row="true"><tr><th>Rank</th><th>Ticker</th><th>Strategy</th><th>Contract</th><th>Expiry</th><th>DTE</th><th>Credit/quote</th><th>Quote as-of</th><th>Risk/ROC</th><th>Earnings</th><th>Confidence</th><th>Sizing</th><th>Conflict</th><th>Rationale</th><th>Source</th><th>Opening validation</th></tr>{tr(['—','—','withheld: no current chain','—','—','—','no current NBBO','—','—','refresh required','not rated','0','historical rows never carried as orders','review-only fail-closed control','three ledgers below','fresh spot, NBBO, DTE, OI, earnings, sweeps, account-risk and US-exchange check'])}</table>

### 4.1a Option Davis 来源摘录 | Option Davis historical source extract ({len(davis_rows)} rows)
<table fit-page-width="true" header-row="true"><tr><th>来源 / as-of</th><th>Ticker</th><th>Strategy</th><th>Stored legs</th><th>Expiry / DTE</th><th>Stored economics</th><th>Quote as-of</th><th>Flow / dark pool</th><th>Score / caution</th><th>Disposition</th></tr>{''.join(davis_rows)}</table>

### 4.1b CSP/BPS 来源摘录 | CSP/BPS historical source extract ({len(csp_rows)} rows)
<table fit-page-width="true" header-row="true"><tr><th>来源 / as-of</th><th>Ticker</th><th>Strategy / status</th><th>Stored legs</th><th>Expiry / DTE</th><th>Stored economics</th><th>Quote as-of</th><th>Structure input</th><th>Caution</th><th>Disposition</th></tr>{''.join(csp_rows)}</table>

### 4.1c UW Income 来源摘录 | UW Income historical source extract ({len(uw_rows)} rows)
<table fit-page-width="true" header-row="true"><tr><th>来源 / as-of</th><th>Ticker</th><th>Strategy</th><th>Stored legs</th><th>Expiry / DTE</th><th>Stored economics</th><th>Quote as-of</th><th>Flow / dark pool</th><th>Confidence / caution</th><th>Disposition</th></tr>{''.join(uw_rows)}</table>

### 4.2 替补与排除 | Alternates & exclusions
- <b>Latest CSP/BPS control:</b> {esc(csp_latest[0].get('reason') if csp_latest else 'not available')}
- <b>Stored Davis:</b> {len(davis)} US-scoped rows; all shown as historical 35-day source marks.
- <b>Stored UW:</b> {len(uw)} US-scoped rows; WHEEL_CC rows do not imply verified share ownership.

### 4.3 四周前瞻与财报后重设 | Four-week queue & post-print re-strike
<table fit-page-width="true" header-row="true"><tr><th>Week</th><th>Catalysts</th><th>Post-print queue</th><th>Permitted strategies</th><th>Gates / holds</th><th>Credit target</th></tr>
{tr(['2026-11-02 to 11-06','attributed earnings / SOFR test','no pre-staged contract','fresh-run dependent','market-open revalidation; no naked gamma across listed events','not allocated'])}
{tr(['2026-11-09 to 11-13','refresh calendar and volatility regime','none preloaded','fresh-run dependent','US-exchange scope + current option chain','not allocated'])}
{tr(['2026-11-16 to 11-20','late-November earnings context','post-print only','defined risk only after fresh scan','do not pre-stage unquoted structures','not allocated'])}
</table>

### 4.4 周度 / 月度预算 | Weekly / monthly budget
<table fit-page-width="true" header-row="true"><tr><th>Week</th><th>Date range</th><th>Events</th><th>Credit target</th><th>Confidence / assumptions</th><th>Notes</th></tr>{tr(['none','review run only','markets closed; current chain unavailable','not calculated','no validated live board','historical ledger credits are not a budget'])}</table>
<b>Monthly total:</b> not calculated; no positions or covered-call income are inferred by this stateless review.

---
## Part 5 · 数据缺口 | Data Gaps
Reconciliation inputs met their configured freshness cadence. The material withheld input is a current option-chain scan, so current spot, NBBO, OI, IV, sweeps, contract-specific earnings, and covered-call ownership cannot be validated. Part 4 source records remain audit evidence only.

---
## Part 6 · 执行要点 | Execution Rules
1. Historical triggers and stored contracts are research conditions only; rerun after the next completed session or use current market-open inputs.
2. Treat a stop as invalidation; do not average down.
3. Any fresh lower effective regime moves every candidate down one tier.
4. A Part 2.3 conflict means skip/rebuild; never call it clean directional evidence.
5. Naked CSP scope is {playbook['naked_csp']} with CSP DTE ≤{playbook['csp_dte_max']} calendar days; every candidate must map to a US exchange.
6. Do not enter inside an earnings block; re-check dates, timing, corporate actions, and other event risk.
7. Stand down when fresh data/contract fields are unavailable or the system-wide invalidation occurs.

---
## 数据来源、方法与免责声明 | Sources, Methodology & Disclaimer
<table fit-page-width="true" header-row="true"><tr><th>来源</th><th>Session / as-of</th><th>Coverage</th><th>Method</th><th>Limitation</th></tr>
{tr(['trade-jlaw-v2',args.date,f'{len(jlaw)} rows; chart {len(chart)}, Yahoo {len(yahoo)}','stored candidate fields','completed-session levels; not live'])}
{tr(['trade-stldc-v2',st_date,f'{len(stldc)} US-listed names; BULL {bull} / BEAR {bear}','alert-to-confirm ladder','not an entry system'])}
{tr(['darkpool_symbol_daily',args.date,f'{len(darkpool)} symbols; net {mm(net_dp)}','daily aggregate mapping','prior-session aggregate'])}
{tr(['flow_verdict_rows + flow-verdicts',args.date,f'{len(flows)}/{len(flows)} rated','handoff + reconciliation','not a current chain'])}
{tr(['option_flow_symbol_signals',unusual_date,f'{len(unusual)} Tier B rows','normalized unusual-flow','absence/insufficiency non-confirmatory'])}
{tr(['option-opportunity-davis',args.date,f'{len(davis)} US-scoped historical rows','Davis ledger','stored marks'])}
{tr(['csp-bps-opportunity',csp_day,f'latest control + {len(old_csp)} archived eligible US rows','CSP/BPS ledger','per-contract historical economics'])}
{tr(['uw-option-opportunities',uw_day,f'{len(uw)} US-scoped historical rows','UW income ledger','stateless; CC ownership not inferred'])}
{tr(['substack-intelligence',f"{intelligence_row.get('edition') or '—'} / {intelligence_row.get('summary_date') or '—'}",'headline + authors','attributed context','not used as price/order source'])}
</table>
研究分析，非投资建议。Research analysis only; not investment advice.
</toggle>
'''
    custom_income = None
    if getattr(args, "income_board_manifest", None):
        from render_weekly_income_board import render as render_income_board
        custom_income = render_income_board(json.loads(Path(args.income_board_manifest).read_text(encoding="utf-8"))).strip()
    elif args.income_board_markdown:
        custom_income = Path(args.income_board_markdown).read_text(encoding="utf-8").strip()
    if not custom_income:
        raise ValueError("Explicit selected-board manifest or validated withheld Part 4 Markdown required; raw source-ledger fallback disabled")
    if custom_income:
        if not custom_income.startswith("## Part 4 ·"):
            raise ValueError("income board Markdown must start with '## Part 4 ·'")
        original_start = report.index("## Part 4 ·")
        original_end = report.index("## Part 5 ·", original_start)
        if args.retain_income_ledgers:
            original_income = report[original_start:original_end]
            ledger_start = original_income.find("### 4.1a ")
            ledger_end = original_income.find("### 4.2 ", ledger_start) if ledger_start >= 0 else -1
            if ledger_start >= 0 and ledger_end >= 0:
                ledgers = original_income[ledger_start:ledger_end]
                ledgers = (ledgers.replace("### 4.1a ", "### 4.1A ")
                                   .replace("### 4.1b ", "### 4.1B ")
                                   .replace("### 4.1c ", "### 4.1C "))
                insertion = custom_income.find("### 4.2 ")
                if insertion < 0:
                    raise ValueError("income board Markdown must contain a '### 4.2 ' section")
                custom_income = custom_income[:insertion] + ledgers + "\n" + custom_income[insertion:]
        report = report[:original_start] + custom_income + "\n\n" + report[original_end:]

    report_path = output / f"daily_trading_plan_v2_{args.date}_review.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only Daily Trading Plan V2 review report")
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--regime-json", required=True)
    parser.add_argument("--reconcile-json", required=True)
    parser.add_argument("--fragility-notes")
    parser.add_argument("--income-board-markdown", help="explicit validated selected/withheld Part 4 Markdown; raw source extracts omitted by default")
    parser.add_argument("--income-board-manifest", help="validated ranked30/20/50 board manifest; preferred over pre-rendered Markdown")
    parser.add_argument("--retain-income-ledgers", action="store_true", help="append full raw income-source extracts to custom Part 4; default is concise selected-board output")
    args = parser.parse_args()
    if not args.income_board_markdown and not args.income_board_manifest:
        parser.error("Provide --income-board-manifest or explicit selected/withheld --income-board-markdown; no raw-ledger fallback")
    try:
        report, snapshot = build(args)
    except (RuntimeError, requests.RequestException, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Review report: {report}")
    print(f"Source snapshot: {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
