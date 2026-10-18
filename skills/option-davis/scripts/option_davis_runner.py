#!/usr/bin/env python3
"""option-davis: daily Davis-playbook option opportunity workflow.

Reads the normalized Supabase stack (flow_verdict_rows, option_flow_symbol_signals
with cross-sectional z-scores, trade-jlaw-v2 option layer, gex_snapshot), prices
Unusual Whales option chains, ranks candidates with the Davis rules, and emits
explicit executable proposals (contract symbol, expiry/DTE, credit, delta,
annualized %) for short puts, bull put spreads, and bear call spreads.

Writes: public."option-opportunity-davis" ledger (PROPOSED rows, merged via
PATCH-by-opportunity_key then INSERT) and a markdown report.

Credentials come from environment variables or a --dotenv file; nothing is
hardcoded. See SKILL.md for the full workflow (including the Notion block step,
which the operator adds after a successful run).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

TABLE = "option-opportunity-davis"

EXP_MAIN = ""
DTE: dict[str, int] = {}
REPORT_DATE = ""
FLOW_DATE = ""
JLAW_DATE = ""
VERDICT_DATE = ""
GEX_FROM = ""
DP_DATE = ""
RUN_ID = uuid.uuid4().hex
NOW_ISO = datetime.now(timezone.utc).isoformat()
LOGGED = 0


def load_env(dotenv_path: str | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    candidates = [Path(p) for p in (dotenv_path, ".env") if p]
    for path in candidates:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")

    def pick(*names: str) -> str:
        for name in names:
            if env.get(name):
                return env[name]
            if os.environ.get(name):
                return os.environ[name]
        return ""

    supabase_url = pick("SUPABASE_URL", "supabase_url")
    supabase_key = pick("SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE", "supabase_service_role")
    uw_key = pick("UW_API_KEY", "UNUSUAL_WHALES_API_KEY", "unusual_whales_api_key")
    return {"SUPABASE_URL": supabase_url, "SUPABASE_KEY": supabase_key, "UW_API_KEY": uw_key}


def next_trading_day(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def pick_expiry(report_date: date) -> tuple[str, int]:
    """Pick the Friday whose DTE from the next trading day is closest to 32."""
    fridays = [
        report_date + timedelta(days=offset)
        for offset in range(25, 40)
        if (report_date + timedelta(days=offset)).weekday() == 4
    ]
    anchor = next_trading_day(report_date)
    best = min(fridays, key=lambda day: abs((day - anchor).days - 32))
    return best.isoformat(), (best - anchor).days


def fnum(value: Any, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def parse_osi(symbol: str) -> tuple[float, str] | None:
    match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    return (float(match.group(4)) / 1000, match.group(3)) if match else None


def build_osi(symbol: str, expiry: str, ctype: str, strike: float) -> str:
    """OCC-style contract symbol: SYMBOL + YYMMDD + C/P + strike*1000 zero-padded to 8."""
    yymmdd = expiry.replace("-", "")[2:]
    return f"{symbol}{yymmdd}{ctype}{strike * 1000:08.0f}"


def sb_get(base: str, key: str, table: str, **params) -> list[dict]:
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    for attempt in range(4):
        try:
            response = requests.get(f"{base}/{table}", headers=headers, params=params, timeout=120)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def uw_get(uw_base: str, uw_key: str, path: str, **params) -> dict:
    headers = {"Authorization": f"Bearer {uw_key}", "UW-CLIENT-API-ID": "100001", "Accept": "application/json"}
    response = requests.get(f"{uw_base}{path}", headers=headers, params=params, timeout=90)
    response.raise_for_status()
    return response.json()


def chain(uw_base: str, uw_key: str, symbol: str, expiry: str):
    quote = uw_get(uw_base, uw_key, f"/api/stock/{symbol}/quote")
    spot = fnum(((quote.get("data") or {}).get("last_trade") or {}).get("price"))
    payload = uw_get(uw_base, uw_key, f"/api/stock/{symbol}/option-contracts", expiry=expiry, limit=500)
    rows = []
    for contract in payload.get("data", []):
        parsed = parse_osi(contract.get("option_symbol", ""))
        if not parsed:
            continue
        strike, ctype = parsed
        rows.append(
            {
                "strike": strike,
                "ctype": ctype,
                "bid": fnum(contract.get("nbbo_bid"), 0.0),
                "ask": fnum(contract.get("nbbo_ask"), 0.0),
                "iv": fnum(contract.get("implied_volatility"), 0.0),
                "delta": fnum(contract.get("delta"), 0.0),
                "oi": fnum(contract.get("open_interest"), 0.0),
            }
        )
    return spot, rows


def near_delta(contracts, side: str, spot: float, target: float):
    pool = [c for c in contracts if c["ctype"] == side and (c["strike"] < spot if side == "P" else c["strike"] > spot)]
    if not pool:
        return None
    return min(pool, key=lambda c: abs(abs(c["delta"]) - target))


def opportunity_row(candidate: dict, strategy: str, short: dict, long: dict | None, spot: float) -> dict | None:
    """Build one ledger row; None when the structure has no positive credit."""
    credit = round(short["bid"], 4) if long is None else round(short["bid"] - long["ask"], 4)
    if credit <= 0:
        return None
    width = round(short["strike"] - long["strike"], 4) if long else None
    collateral = round(short["strike"] - credit, 4) if long is None else round(width - credit, 4)
    max_loss = collateral if long is None else round(width - credit, 4)
    ror = round(credit / max_loss, 4) if max_loss else None
    ann = round(credit / short["strike"] / DTE[EXP_MAIN] * 365 * 100, 2)
    delta = abs(short["delta"])
    cautions = []
    if candidate["verdict"] == "mixed":
        cautions.append("VERDICT_MIXED")
    if (candidate.get("net_bias_z") or 0) < 0:
        cautions.append("FLOW_CONFLICT")
    if ann < 20:
        cautions.append("ANNUALIZED_BELOW_20")
    if not (0.20 <= delta <= 0.35):
        cautions.append("DELTA_OUT_OF_BAND")
    if (short["oi"] or 0) < 20:
        cautions.append("THIN_OI")
    if not candidate.get("earn_ok"):
        cautions.append("EARNINGS_INSIDE_WINDOW")
    if candidate.get("dp") is None:
        cautions.append("DP_NO_DATA")
    elif candidate.get("dp_accumulating") is False and strategy != "BEAR_CALL_SPREAD":
        cautions.append("DP_DISTRIBUTION")
    side_letter = "C" if strategy == "BEAR_CALL_SPREAD" else "P"
    row = {
        "opportunity_key": f"{REPORT_DATE}|{strategy}|{candidate['symbol']}|{EXP_MAIN}|{short['strike']:.0f}",
        "run_id": RUN_ID,
        "report_date": REPORT_DATE,
        "status": "PROPOSED",
        "strategy": strategy,
        "symbol": candidate["symbol"],
        "side": "call" if strategy == "BEAR_CALL_SPREAD" else "put",
        "expiry": EXP_MAIN,
        "dte": DTE[EXP_MAIN],
        "short_strike": short["strike"],
        "short_delta": round(delta, 4),
        "short_oi": short["oi"],
        "short_option_symbol": build_osi(candidate["symbol"], EXP_MAIN, side_letter, short["strike"]),
        "long_strike": long["strike"] if long else None,
        "long_delta": round(abs(long["delta"]), 4) if long else None,
        "long_option_symbol": build_osi(candidate["symbol"], EXP_MAIN, side_letter, long["strike"]) if long else None,
        "spread_width": width,
        "spot_price": round(spot, 2),
        "quote_as_of": NOW_ISO,
        "quote_status": "live" if short["bid"] > 0 else "stale",
        "verdict": candidate["verdict"],
        "verdict_reasons": candidate.get("reasons") or [],
        "key_flows": candidate.get("key_flows") or [],
        "flow_signal_date": FLOW_DATE,
        "flow_bias": candidate.get("flow_bias"),
        "bullish_z": candidate.get("bullish_z"),
        "bearish_z": candidate.get("bearish_z"),
        "net_bias_z": candidate.get("net_bias_z"),
        "jlaw_run_date": JLAW_DATE,
        "jlaw_classification": candidate.get("class"),
        "jlaw_score": int(candidate["jlaw"]) if candidate.get("jlaw") is not None else None,
        "option_score": int(candidate["option_score"]) if candidate.get("option_score") is not None else None,
        "option_tier": candidate.get("tier"),
        "option_alert": candidate.get("alert"),
        "option_iv30": candidate.get("iv30"),
        "option_iv_rank": candidate.get("iv_rank"),
        "option_iv_trend": candidate.get("iv_trend"),
        "option_put_call_ratio": candidate.get("pcr"),
        "option_pct_below_high": candidate.get("below_high"),
        "gex_snapshot_ts": candidate.get("gex_ts"),
        "gex_put_wall": candidate.get("put_wall"),
        "gex_call_wall": candidate.get("call_wall"),
        "gex_net_mm": candidate.get("gex_net"),
        "gex_gravity": candidate.get("gravity"),
        "put_wall_pct": candidate.get("pw_pct"),
        "call_wall_pct": candidate.get("cw_pct"),
        "earnings_date": candidate.get("earn"),
        "earnings_clear": candidate.get("earn_ok"),
        "short_bid": short["bid"],
        "short_ask": short["ask"],
        "long_bid": long["bid"] if long else None,
        "long_ask": long["ask"] if long else None,
        "entry_credit": credit,
        "collateral": collateral,
        "max_loss": max_loss,
        "return_on_risk": ror,
        "annualized_pct": ann,
        "davis_annualized_ok": ann >= 20,
        "delta_band_ok": 0.20 <= delta <= 0.35,
        "score": candidate["score"],
        "caution": "; ".join(cautions) or None,
        "notes": "Quotes = latest session close; re-verify before entry.",
    }
    if candidate.get("dp"):
        row.update(candidate["dp"])
    else:
        # The daily aggregate table is the single source of truth: explicitly
        # null dp_* so legacy values from the old patch mode cannot linger.
        for key in (
            "dp_as_of", "dp_total_premium_mm", "dp_buy_mm", "dp_sell_mm",
            "dp_net_mm", "dp_blocks", "dp_top_level", "dp_top_level_mm", "dp_accumulating",
        ):
            row[key] = None
    return row


def upsert_opportunity(base: str, key: str, row: dict) -> int:
    """Merge one ledger row: PATCH by opportunity_key, INSERT when nothing matched.

    PostgREST's merge-duplicates upsert is not honored on this table (identity
    PK + unique constraint), so the merge is explicit. Returns 1 when the row
    was written or updated.
    """
    global LOGGED
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    patch = requests.patch(
        f"{base}/{TABLE}",
        headers={**headers, "Prefer": "return=representation"},
        params={"opportunity_key": f"eq.{row['opportunity_key']}"},
        json=row,
        timeout=60,
    )
    if patch.status_code == 200 and patch.json():
        LOGGED += 1
        return 1
    response = requests.post(f"{base}/{TABLE}", headers=headers, json=[row], timeout=60)
    if response.status_code >= 400:
        print(f"LEDGER WRITE FAILED ({response.status_code}): {response.text[:500]}")
        sys.exit(2)
    LOGGED += 1
    return 1


def run(cfg: dict, args: argparse.Namespace) -> int:
    global LOGGED, EXP_MAIN, DTE, REPORT_DATE, FLOW_DATE, JLAW_DATE, VERDICT_DATE, GEX_FROM, NOW_ISO
    base = cfg["SUPABASE_URL"].rstrip("/")
    if not base.endswith("/rest/v1"):
        base += "/rest/v1"
    report_date = date.fromisoformat(args.report_date) if args.report_date else date.today()
    REPORT_DATE = report_date.isoformat()
    EXP_MAIN, dte = pick_expiry(report_date)
    DTE[EXP_MAIN] = dte
    NOW_ISO = datetime.now(timezone.utc).isoformat()
    print(f"report_date={REPORT_DATE} expiry={EXP_MAIN} dte={dte}")

    def max_date(table: str, column: str) -> str | None:
        rows = sb_get(base, cfg["SUPABASE_KEY"], table, select=column, order=f"{column}.desc", limit="1")
        return str(rows[0][column])[:10] if rows else None

    JLAW_DATE = max_date("trade-jlaw-v2", "run_date") or REPORT_DATE
    FLOW_DATE = max_date("option_flow_symbol_signals", "signal_date") or REPORT_DATE
    VERDICT_DATE = max_date("flow_verdict_rows", "verdict_date") or REPORT_DATE
    gex_top = sb_get(base, cfg["SUPABASE_KEY"], "gex_snapshot", select="snapshot_ts", order="snapshot_ts.desc", limit="1")
    gex_latest = str(gex_top[0]["snapshot_ts"])[:10] if gex_top else REPORT_DATE
    GEX_FROM = (date.fromisoformat(gex_latest) - timedelta(days=1)).isoformat()
    print(f"data dates: jlaw={JLAW_DATE} flow={FLOW_DATE} verdicts={VERDICT_DATE} gex>={GEX_FROM}")

    jlaw = sb_get(base, cfg["SUPABASE_KEY"], "trade-jlaw-v2", select="*", run_date=f"eq.{JLAW_DATE}")
    flow = sb_get(base, cfg["SUPABASE_KEY"], "option_flow_symbol_signals", select="*", signal_date=f"eq.{FLOW_DATE}")
    verdicts = sb_get(base, cfg["SUPABASE_KEY"], "flow_verdict_rows", select="*", verdict_date=f"eq.{VERDICT_DATE}")
    gex = sb_get(base, cfg["SUPABASE_KEY"], "gex_snapshot", select="*", snapshot_ts=f"gte.{GEX_FROM}", order="snapshot_ts.asc")

    # Dark pool enricher: join the latest darkpool_symbol_daily aggregates.
    # Absent table/rows degrades to DP_NO_DATA cautions instead of failing.
    dp_idx: dict[str, dict] = {}
    try:
        dp_top = sb_get(base, cfg["SUPABASE_KEY"], "darkpool_symbol_daily", select="fetch_date", order="fetch_date.desc", limit="1")
        if dp_top:
            DP_DATE = str(dp_top[0]["fetch_date"])[:10]
            dp_rows = sb_get(base, cfg["SUPABASE_KEY"], "darkpool_symbol_daily", select="*", fetch_date=f"eq.{DP_DATE}")
            for row in dp_rows:
                if row.get("symbol"):
                    dp_idx[row["symbol"]] = row
            print(f"darkpool: date={DP_DATE} symbols={len(dp_idx)}")
        else:
            print("darkpool: no rows — dark pool signals disabled")
    except Exception as exc:
        print(f"darkpool load failed ({type(exc).__name__}) — dark pool signals disabled")

    j_idx, f_idx, v_idx, g_idx = {}, {}, {}, {}
    for row in jlaw:
        symbol = row.get("symbol")
        if symbol not in j_idx or (row.get("jlaw_score") or 0) > (j_idx[symbol].get("jlaw_score") or 0):
            j_idx[symbol] = row
    for row in flow:
        symbol = row.get("ticker")
        if symbol not in f_idx or (row.get("net_bias_z") or -999) > (f_idx[symbol].get("net_bias_z") or -999):
            f_idx[symbol] = row
    for row in verdicts:
        v_idx[row.get("symbol")] = row
    for row in gex:
        g_idx[row.get("symbol")] = row
    print(f"loaded: jlaw={len(j_idx)} flow={len(f_idx)} verdicts={len(v_idx)} gex={len(g_idx)}")

    def enrich(symbol: str, verdict: dict, jlaw_row: dict, flow_row: dict | None, gex_row: dict | None, dp_row: dict | None) -> dict:
        iv_rank = fnum(jlaw_row.get("option_iv_rank"), 0.0)
        spot = fnum(jlaw_row.get("current_price")) or fnum((gex_row or {}).get("spot_price"))
        put_wall = fnum((gex_row or {}).get("put_wall"))
        call_wall = fnum((gex_row or {}).get("call_wall"))
        pw = round(100 * (spot - put_wall) / spot, 1) if put_wall and spot and put_wall < spot else None
        cw = round(100 * (call_wall - spot) / spot, 1) if call_wall and spot and call_wall > spot else None
        dp_net_mm = round(fnum((dp_row or {}).get("net_premium"), 0.0) / 1e6, 2) if dp_row else None
        dp_map = None
        if dp_row:
            dp_map = {
                "dp_as_of": dp_row.get("window_end") or dp_row.get("fetch_date"),
                "dp_total_premium_mm": round(fnum(dp_row.get("total_premium"), 0.0) / 1e6, 2),
                "dp_buy_mm": round(fnum(dp_row.get("buy_premium"), 0.0) / 1e6, 2),
                "dp_sell_mm": round(fnum(dp_row.get("sell_premium"), 0.0) / 1e6, 2),
                "dp_net_mm": dp_net_mm,
                "dp_blocks": int(fnum(dp_row.get("block_count"), 0)),
                "dp_top_level": fnum(dp_row.get("top_level")),
                "dp_top_level_mm": round(fnum(dp_row.get("top_level_premium"), 0.0) / 1e6, 2),
                "dp_accumulating": bool(dp_row.get("accumulating")),
            }
        return {
            "symbol": symbol,
            "verdict": verdict["verdict"],
            "spot": spot,
            "iv_rank": round(iv_rank, 0),
            "iv30": fnum(jlaw_row.get("option_iv30"), 0.0),
            "tier": jlaw_row.get("option_tier"),
            "alert": jlaw_row.get("option_alert"),
            "jlaw": jlaw_row.get("jlaw_score"),
            "class": jlaw_row.get("classification"),
            "option_score": jlaw_row.get("option_score"),
            "iv_trend": jlaw_row.get("option_iv_trend"),
            "pcr": fnum(jlaw_row.get("option_put_call_ratio")),
            "below_high": fnum(jlaw_row.get("option_pct_below_high")),
            "put_wall": put_wall,
            "call_wall": call_wall,
            "pw_pct": pw,
            "cw_pct": cw,
            "flow_bias": (flow_row or {}).get("net_option_flow_bias"),
            "bullish_z": fnum((flow_row or {}).get("bullish_z")),
            "bearish_z": fnum((flow_row or {}).get("bearish_z")),
            "net_bias_z": fnum((flow_row or {}).get("net_bias_z")),
            "earn": verdict.get("next_earnings_date"),
            "earn_ok": (not verdict.get("next_earnings_date")) or str(verdict["next_earnings_date"]) > EXP_MAIN,
            "reasons": verdict.get("reasons"),
            "key_flows": verdict.get("key_flows"),
            "gex_ts": (gex_row or {}).get("snapshot_ts"),
            "gex_net": fnum((gex_row or {}).get("net_gex_mm")),
            "gravity": (gex_row or {}).get("gravity"),
            "dp": dp_map,
            "dp_net_mm": dp_net_mm,
            "dp_accumulating": bool(dp_row.get("accumulating")) if dp_row else None,
        }

    bull = []
    for verdict in v_idx.values():
        if verdict.get("verdict") not in ("bullish", "mixed") or verdict.get("earnings_block"):
            continue
        symbol = verdict["symbol"]
        jlaw_row, flow_row, gex_row = j_idx.get(symbol), f_idx.get(symbol), g_idx.get(symbol)
        if not jlaw_row or jlaw_row.get("option_score") is None:
            continue
        candidate = enrich(symbol, verdict, jlaw_row, flow_row, gex_row, dp_idx.get(symbol))
        if not candidate["spot"] or candidate["iv_rank"] < 15 or candidate["iv_rank"] > 85:
            continue
        score = 0.0
        score += 3 if verdict.get("verdict") == "bullish" else 1
        jlaw_score = jlaw_row.get("jlaw_score") or 0
        score += 3 if jlaw_score >= 9 else (1.5 if jlaw_score >= 7 else 0)
        score += 1 if jlaw_row.get("classification") == "Watch" else 0
        score += 2 if jlaw_row.get("option_alert") else 0
        score += 1.5 if jlaw_row.get("option_tier") in ("MODERATE", "STRONG") else 0
        score += 2 if (flow_row or {}).get("net_option_flow_bias") == "bullish" else 0
        score += 1 if fnum((flow_row or {}).get("net_bias_z"), 0.0) >= 1.0 else 0
        score += 1.5 if candidate["pw_pct"] is not None and 0.5 <= candidate["pw_pct"] <= 6 else 0
        score += 1 if 20 <= candidate["iv_rank"] <= 80 else 0
        score += 1 if candidate["earn_ok"] else 0
        score += 1.5 if candidate["dp_accumulating"] is True else 0  # dark pool accumulation confirms
        gates = jlaw_row.get("gates") or {}
        score += 0.5 if gates.get("trend_ok") else 0
        score += 0.5 if gates.get("near_52w_high") else 0
        candidate["score"] = score
        bull.append(candidate)
    bull.sort(key=lambda item: (-item["score"], -(item["jlaw"] or 0)))

    bear = []
    for verdict in v_idx.values():
        if verdict.get("verdict") != "bearish" or verdict.get("earnings_block"):
            continue
        symbol = verdict["symbol"]
        candidate = enrich(symbol, verdict, j_idx.get(symbol, {}), f_idx.get(symbol), g_idx.get(symbol), dp_idx.get(symbol))
        if not candidate["spot"]:
            continue
        candidate["score"] = 0.0
        bear.append(candidate)
    # Rank bear side by bearish flow z, with dark-pool distribution as the tiebreak.
    bear.sort(key=lambda item: (-(item["bearish_z"] or -999), 0 if item["dp_accumulating"] is False else 1))
    print(f"bull candidates: {len(bull)} bear candidates: {len(bear)}")

    out: list[str] = [f"# Option Trading Opportunities — {REPORT_DATE}", ""]
    data_line = (
        f"Data: flow_verdict_rows {VERDICT_DATE}, option_flow_symbol_signals {FLOW_DATE} (normalized), "
        f"trade-jlaw-v2 {JLAW_DATE} (option layer), gex_snapshot >= {GEX_FROM}"
    )
    if DP_DATE:
        data_line += f", darkpool_symbol_daily {DP_DATE}"
    out.append(data_line + ". Chain quotes = latest session close; re-verify before entry.")
    out.append("")
    out.append("## Short puts (bullish side) — executable proposals")
    out.append("")
    out.append("| symbol | spot | score | earn | expiry (DTE) | 30Δ contract | credit | Δ | ann% | 20Δ contract | credit | Δ | ann% | dark pool |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    published = 0
    spread_rows = []
    for candidate in [c for c in bull if c["earn_ok"]][:14]:
        try:
            spot, contracts = chain(cfg["UW_BASE"], cfg["UW_API_KEY"], candidate["symbol"], EXP_MAIN)
        except Exception as exc:
            print(f"  chain {candidate['symbol']} failed: {exc}")
            continue
        if not spot:
            continue
        p30 = near_delta(contracts, "P", spot, 0.30)
        p20 = near_delta(contracts, "P", spot, 0.20)
        long30 = None
        if p30:
            long30 = next((x for x in contracts if x["ctype"] == "P" and abs(x["strike"] - (p30["strike"] - 5)) < 0.01), None)

        def money(p):
            return f"{(p['bid'] + p['ask']) / 2:.2f}" if p else "—"

        def delta(p):
            return f"{abs(p['delta']):.2f}" if p else "—"

        def ann(p):
            return f"{(p['bid'] + p['ask']) / 2 / p['strike'] / DTE[EXP_MAIN] * 365 * 100:.0f}%" if p else "—"

        osi = lambda strike: build_osi(candidate["symbol"], EXP_MAIN, "P", strike)

        def dp_cell(c):
            if c.get("dp_net_mm") is None:
                return "—"
            tag = "acc" if c.get("dp_accumulating") else "dist"
            return f"{c['dp_net_mm']:+.1f}M {tag}"

        out.append(
            f"| {candidate['symbol']} | {spot:.2f} | {candidate['score']:.1f} | {candidate['earn'] or '—'} | "
            f"{EXP_MAIN} ({DTE[EXP_MAIN]}) | {osi(p30['strike']) if p30 else '—'} | {money(p30)} | {delta(p30)} | {ann(p30)} | "
            f"{osi(p20['strike']) if p20 else '—'} | {money(p20)} | {delta(p20)} | {ann(p20)} | {dp_cell(candidate)} |"
        )
        if p30 and long30:
            credit = (p30["bid"] + p30["ask"]) / 2 - (long30["bid"] + long30["ask"]) / 2
            if credit > 0:
                spread_rows.append((candidate, spot, p30, long30, credit))
        if p30 and not args.no_log:
            row = opportunity_row(candidate, "SHORT_PUT", p30, None, spot)
            if row:
                published += upsert_opportunity(base, cfg["SUPABASE_KEY"], row)
        if long30 and any(s[0] is candidate for s in spread_rows) and not args.no_log:
            row = opportunity_row(candidate, "BULL_PUT_SPREAD", p30, long30, spot)
            if row:
                published += upsert_opportunity(base, cfg["SUPABASE_KEY"], row)
        time.sleep(0.3)

    out.append("")
    out.append("## Bull put spreads (5-wide) — executable proposals")
    out.append("")
    out.append("| symbol | spot | expiry (DTE) | short contract | long contract | net credit | max loss | ROC |")
    out.append("|---|---|---|---|---|---|---|---|")
    for candidate, spot, p30, long30, credit in spread_rows:
        max_loss = round(5 - credit, 2)
        out.append(
            f"| {candidate['symbol']} | {spot:.2f} | {EXP_MAIN} ({DTE[EXP_MAIN]}) | "
            f"{build_osi(candidate['symbol'], EXP_MAIN, 'P', p30['strike'])} | "
            f"{build_osi(candidate['symbol'], EXP_MAIN, 'P', long30['strike'])} | "
            f"{credit:.2f} | {max_loss:.2f} | {credit / max_loss * 100:.0f}% |"
        )

    out.append("")
    out.append("## Post-earnings watchlist (strong data, earnings inside the window — sell puts AFTER the print)")
    out.append("")
    out.append("| symbol | verdict | score | earn | why |")
    out.append("|---|---|---|---|---|")
    for candidate in [c for c in bull if not c["earn_ok"]][:10]:
        why = f"alert={candidate['alert']}, tier={candidate['tier']}, ivR={candidate['iv_rank']:.0f}, flow={candidate['flow_bias'] or '-'}"
        out.append(f"| {candidate['symbol']} | {candidate['verdict']} | {candidate['score']:.1f} | {candidate['earn'] or '-'} | {why} |")

    out.append("")
    out.append("## Bear call spreads — executable proposals")
    out.append("")
    out.append("| symbol | spot | expiry (DTE) | 30Δ contract | credit | Δ | 20Δ contract | credit | Δ | dp net |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for candidate in bear[:6]:
        try:
            spot, contracts = chain(cfg["UW_BASE"], cfg["UW_API_KEY"], candidate["symbol"], EXP_MAIN)
        except Exception as exc:
            print(f"  chain {candidate['symbol']} failed: {exc}")
            continue
        if not spot:
            continue
        c30 = near_delta(contracts, "C", spot, 0.30)
        c20 = near_delta(contracts, "C", spot, 0.20)
        long30 = next((x for x in contracts if x["ctype"] == "C" and abs(x["strike"] - (c30["strike"] + 5)) < 0.01), None) if c30 else None

        def money(p):
            return f"{(p['bid'] + p['ask']) / 2:.2f}" if p else "—"

        def delta(p):
            return f"{p['delta']:.2f}" if p else "—"

        osi = lambda strike: build_osi(candidate["symbol"], EXP_MAIN, "C", strike)
        dp_net_cell = f"{candidate['dp_net_mm']:+.1f}M" if candidate.get("dp_net_mm") is not None else "—"
        out.append(
            f"| {candidate['symbol']} | {spot:.2f} | {EXP_MAIN} ({DTE[EXP_MAIN]}) | "
            f"{osi(c30['strike']) if c30 else '—'} | {money(c30)} | {delta(c30)} | "
            f"{osi(c20['strike']) if c20 else '—'} | {money(c20)} | {delta(c20)} | {dp_net_cell} |"
        )
        if c30 and long30 and not args.no_log:
            row = opportunity_row(candidate, "BEAR_CALL_SPREAD", c30, long30, spot)
            if row:
                published += upsert_opportunity(base, cfg["SUPABASE_KEY"], row)
        time.sleep(0.3)

    out.append("")
    out.append("Davis guardrails: entry on a red/oversold day only; strike below support (GEX put wall); 20-30 delta;")
    out.append("DTE <= 45; annualized premium >= 20% filter; risk 5-7% of capital per trade via BPR; exit/decide at 21 DTE.")
    out.append("")
    out.append(f"Ledger: {'skipped (--no-log)' if args.no_log else str(published) + ' rows upserted to public.option-opportunity-davis'}.")
    report = "\n".join(out) + "\n"
    report_path = Path.cwd() / f"opportunities-{REPORT_DATE}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nreport -> {report_path.resolve()}\n")
    print(report)
    print(f"ledger rows published: {published}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="option-davis daily opportunity workflow.")
    parser.add_argument("--report-date", default=None, help="Publication date YYYY-MM-DD (default: today).")
    parser.add_argument("--no-log", action="store_true", help="Report only; do not write the ledger.")
    parser.add_argument("--dotenv", default=None, help="Path to a .env file with Supabase and Unusual Whales keys.")
    args = parser.parse_args()

    env = load_env(args.dotenv)
    missing = [name for name in ("SUPABASE_URL", "SUPABASE_KEY", "UW_API_KEY") if not env[name]]
    if missing:
        sys.exit(f"Missing credentials: {', '.join(missing)}. Provide them via environment variables or --dotenv.")
    cfg = {
        "SUPABASE_URL": env["SUPABASE_URL"],
        "SUPABASE_KEY": env["SUPABASE_KEY"],
        "UW_BASE": env.get("UW_BASE") or "https://api.unusualwhales.com",
        "UW_API_KEY": env["UW_API_KEY"],
    }
    return run(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
