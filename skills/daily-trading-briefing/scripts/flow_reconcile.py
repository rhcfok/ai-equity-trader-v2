#!/usr/bin/env python3
"""flow_reconcile.py: data freshness, flow-source reconciliation and board label check.

Three jobs for Step 0 / Step 2 / pre-publish of daily-trading-briefing:

1. Data freshness (replaces the hand-written "data gaps" list)
   Latest date per upstream table and age in weekdays, plus a duplicate
   dark-pool snapshot check (2026-10-25 and 10-26 had identical 147-row,
   -$2.03B snapshots). The 10-29 briefing called option_flow_symbol_signals
   "stale since 10-13" for five days while it had rows through 10-28. This
   report is computed, so that cannot happen again.

2. Flow reconciliation per ticker
   Reuses compute_flow_composite() from uw-options-income-engine (single
   source of truth) over flow_verdict_rows (agent verdict + uncapped alert
   stats), option_flow_symbol_signals (premium tilt, 3-day persistence, robust
   net_bias_z) and darkpool_symbol_daily. Each ticker gets a status:
     confirmed  verdict, composite label and dark pool (when |dp| >= 0.2) agree
     conflict   any flow_conflicts (verdict_vs_premium, darkpool_vs_options,
                verdict_vs_alert_premium) -> must be written in the briefing
     partial    everything else
   Conflicts are listed, not netted away.

3. Board label check (--check-board <briefing.md>)
   Flags any line that states a verdict for a ticker that differs from
   flow_verdict_rows (e.g. "PANW ... bullish verdict" when the row is mixed),
   any "verdict 待核 / pending" for a ticker that has a stored verdict (CAT),
   and any staleness claim about a table that the freshness report shows as
   fresh. --strict exits 1 when issues are found (run before the Notion push).

Usage:
    python skills/daily-trading-briefing/scripts/flow_reconcile.py --date 2026-10-29 --out .
    python skills/daily-trading-briefing/scripts/flow_reconcile.py --date 2026-10-29 \\
        --check-board briefing_2026-10-29.md --strict

Outputs flow_reconcile_<date>.json and flow_reconcile_<date>.md (bilingual
section, paste into the briefing's flow / data-gap sections).

Env: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (or lower-case supabase_url /
supabase_service_role in ./.env). --inputs-json runs fully offline from a
pre-fetched bundle (same shape as fetch_inputs()).
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from regime_score import _get, _rest_base, load_env  # noqa: E402

_ENGINE_PATH = HERE.parents[1] / "uw-options-income-engine" / "scripts" / "uw_options_income_runner.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location("uw_options_income_runner", _ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {_ENGINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


engine = _load_engine()

# table -> (date column, max age in weekdays before "stale")
FRESHNESS_RULES: dict[str, tuple[str, int]] = {
    "flow_verdict_rows": ("verdict_date", 1),
    "flow-verdicts": ("verdict_date", 1),
    "option_flow_symbol_signals": ("signal_date", 2),  # prior completed session
    "darkpool_symbol_daily": ("fetch_date", 1),
    "gex_snapshot": ("snapshot_ts", 1),
    "trade-jlaw-v2": ("run_date", 2),  # chart pipeline often lags a day
    "trade-stldc-v2": ("run_date", 2),
    "market_regime_daily": ("report_date", 1),
}

VERDICTS = ("bullish", "bearish", "mixed", "protective", "no-data", "covered-call-selling")
DIR = {"bullish": 1, "bearish": -1, "protective": -1, "mixed": 0, "no-data": 0, "covered-call-selling": 0}
LABEL_DIR = {"strong_bull": 1, "bull": 1, "neutral": 0, "bear": -1, "strong_bear": -1}
DP_MIN = 0.2


# ---------------------------------------------------------------------------
# 1. Freshness
# ---------------------------------------------------------------------------


def weekdays_between(older: date, newer: date) -> int:
    """Weekdays strictly after `older` up to and including `newer` (0 if same day)."""
    if newer <= older:
        return 0
    n, d = 0, older
    while d < newer:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def freshness_report(latest: dict[str, str | None], as_of: date) -> list[dict[str, Any]]:
    out = []
    for table, (col, max_age) in FRESHNESS_RULES.items():
        if table not in latest:
            continue
        raw = latest.get(table)
        d = engine.parse_iso_date(str(raw)[:10]) if raw else None
        age = weekdays_between(d, as_of) if d else None
        status = "missing" if d is None else ("ok" if age <= max_age else "stale")
        out.append({"table": table, "column": col, "latest": d.isoformat() if d else None, "age_weekdays": age, "max_age": max_age, "status": status})
    return out


def darkpool_duplicate(summaries: list[dict[str, Any]]) -> list[str]:
    """summaries: [{fetch_date, rows, net, total}] newest first; flags identical consecutive snapshots."""
    flags = []
    for cur, prev in zip(summaries, summaries[1:]):
        if (
            cur.get("rows") == prev.get("rows")
            and round(float(cur.get("net") or 0), 0) == round(float(prev.get("net") or 0), 0)
            and round(float(cur.get("total") or 0), 0) == round(float(prev.get("total") or 0), 0)
        ):
            flags.append(f"{cur['fetch_date']} == {prev['fetch_date']}")
    return flags


# ---------------------------------------------------------------------------
# 2. Reconciliation
# ---------------------------------------------------------------------------


def build_bundle(sym: str, vrow: dict | None, sig_rows: list[dict], dp_row: dict | None):
    b = engine.SymbolSignalBundle(symbol=sym)
    if vrow:
        b.verdict = str(vrow.get("verdict") or "no-data")
        b.verdict_date = str(vrow.get("verdict_date") or "")[:10] or None
        b.flow_alert_count = engine.safe_int(vrow.get("alert_count"))
        b.alert_count_total = engine.safe_int(vrow.get("alert_count_total"))
        b.alert_premium_tilt = engine.safe_float(vrow.get("premium_tilt"))
        b.verdict_quant = vrow.get("verdict_quant")
    hist = sorted(sig_rows, key=lambda r: str(r.get("signal_date")), reverse=True)
    b.flow_history = hist[:3]
    if hist:
        b.flow_signal_date = str(hist[0].get("signal_date"))[:10]
        b.net_bias_z = engine.safe_float(hist[0].get("net_bias_z"))
    if dp_row:
        b.dp_total_premium_mm = round((engine.safe_float(dp_row.get("total_premium")) or 0.0) / 1e6, 2)
        b.dp_net_mm = round((engine.safe_float(dp_row.get("net_premium")) or 0.0) / 1e6, 2)
    return b


def reconcile_symbol(sym: str, vrow: dict | None, sig_rows: list[dict], dp_row: dict | None, as_of: date) -> dict[str, Any]:
    b = build_bundle(sym, vrow, sig_rows, dp_row)
    engine.compute_flow_composite(b, as_of)
    comps = b.flow_components or {}
    v_dir = 0 if b.verdict_stale else DIR.get(str(b.verdict).lower(), 0)
    c_dir = LABEL_DIR.get(b.flow_label, 0)
    dp = comps.get("darkpool")
    dp_dir = 0 if dp is None or abs(dp) < DP_MIN else (1 if dp > 0 else -1)
    if b.flow_conflicts:
        status = "conflict"
    elif v_dir != 0 and c_dir == v_dir and dp_dir == v_dir:
        status = "confirmed"
    else:
        status = "partial"
    return {
        "symbol": sym,
        "verdict": b.verdict,
        "verdict_date": b.verdict_date,
        "verdict_stale": b.verdict_stale,
        "verdict_quant": b.verdict_quant,
        "alert_count": b.flow_alert_count,
        "alert_count_total": b.alert_count_total,
        "alert_premium_tilt": b.alert_premium_tilt,
        "net_bias_z": b.net_bias_z,
        "flow_signal_date": b.flow_signal_date,
        "premium_today": comps.get("premium_today"),
        "premium_3d": comps.get("premium_3d"),
        "darkpool": dp,
        "dp_net_mm": b.dp_net_mm,
        "flow_composite": b.flow_composite,
        "flow_label": b.flow_label,
        "flow_conflicts": b.flow_conflicts,
        "status": status,
    }


def reconcile(inputs: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    verdicts = {str(r["symbol"]).upper(): r for r in inputs.get("verdict_rows", [])}
    sigs: dict[str, list[dict]] = {}
    for r in inputs.get("signal_rows", []):
        if str(r.get("signal_date") or "") <= as_of.isoformat():
            sigs.setdefault(str(r.get("ticker") or "").upper(), []).append(r)
    dps = {str(r["symbol"]).upper(): r for r in inputs.get("darkpool_rows", [])}
    universe = sorted(set(verdicts) | set(sigs)) if not inputs.get("symbols") else [s.upper() for s in inputs["symbols"]]
    return [reconcile_symbol(s, verdicts.get(s), sigs.get(s, []), dps.get(s), as_of) for s in universe]


# ---------------------------------------------------------------------------
# 3. Board label check
# ---------------------------------------------------------------------------

_CLAIM = re.compile(
    r"(?P<pre>bullish|bearish|mixed|protective|no-data)\s*verdict"
    r"|verdict\s*[:：=]?\s*(?P<post>bullish|bearish|mixed|protective|no-data)"
    r"|verdict\s*(?P<pending>待核|待定|pending|tbd|待确认)",
    re.IGNORECASE,
)
_STALE = re.compile(r"stale|停更|断档|未更新|not updated", re.IGNORECASE)
_TICKER = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9.]{0,5})(?![A-Za-z0-9_])")


def _records(text: str) -> list[str]:
    """Split a briefing into records: Notion <tr> rows when present, else lines."""
    t = text.replace("\\_", "_")
    parts = re.split(r"<tr[^>]*>", t, flags=re.IGNORECASE) if re.search(r"<tr[\s>]", t, re.IGNORECASE) else [t]
    out: list[str] = []
    for part in parts:
        if "<tr" in text.lower():
            part = part.split("</tr>")[0] if "</tr>" in part.lower() else part
        for line in (part.splitlines() if not re.search(r"<td", part, re.IGNORECASE) else [part]):
            clean = html.unescape(re.sub(r"<[^>]+>", " ", line))
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                out.append(clean)
    return out


def check_board(text: str, verdicts: dict[str, str], freshness: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    known = {s.upper() for s in verdicts}
    fresh_tables = {f["table"]: f for f in (freshness or []) if f["status"] == "ok"}
    for rec in _records(text):
        tickers = [(m.start(), m.group(1)) for m in _TICKER.finditer(rec) if m.group(1) in known]
        for m in _CLAIM.finditer(rec):
            before = [t for pos, t in tickers if pos < m.start()]
            if not before:
                continue
            sym = before[-1]
            stored = str(verdicts.get(sym) or "").lower()
            snippet = rec[max(0, m.start() - 60) : m.end() + 20]
            if m.group("pending"):
                if stored:
                    issues.append({"type": "pending_but_stored", "symbol": sym, "claimed": "pending", "stored": stored, "text": snippet})
                continue
            claimed = (m.group("pre") or m.group("post") or "").lower()
            if stored and claimed != stored:
                issues.append({"type": "verdict_mismatch", "symbol": sym, "claimed": claimed, "stored": stored, "text": snippet})
        if _STALE.search(rec):
            for table, f in fresh_tables.items():
                for tm in re.finditer(re.escape(table) + r"(?![\w-])", rec):
                    # the claim follows the table name, within the same list item
                    window = re.split(r"[；;|。•]", rec[tm.end() : tm.end() + 60])[0]
                    if _STALE.search(window):
                        issues.append({"type": "false_staleness", "symbol": None, "table": table, "latest": f["latest"], "text": table + window})
                        break
    return issues


# ---------------------------------------------------------------------------
# Supabase fetch
# ---------------------------------------------------------------------------


def fetch_inputs(report_date: str) -> dict[str, Any]:
    import requests

    env = load_env()
    if not env["url"] or not env["key"]:
        raise SystemExit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (env or .env)")
    base = _rest_base(env["url"])
    s = requests.Session()
    s.headers.update({"apikey": env["key"], "Authorization": f"Bearer {env['key']}"})

    def latest(table: str, col: str) -> str | None:
        try:
            rows = _get(s, f"{base}/{table}", {"select": col, col: f"lte.{report_date}T23:59:59" if col.endswith("_ts") else f"lte.{report_date}", "order": f"{col}.desc", "limit": "1"}).json()
        except Exception:  # noqa: BLE001 - table may not exist in every project
            return None
        return str(rows[0][col])[:10] if rows else None

    latest_dates = {t: latest(t, c) for t, (c, _) in FRESHNESS_RULES.items()}
    vdate = latest_dates.get("flow_verdict_rows") or report_date
    verdict_rows = _get(s, f"{base}/flow_verdict_rows", {"select": "*", "verdict_date": f"eq.{vdate}", "limit": "2000"}).json()
    since = (date.fromisoformat(report_date) - timedelta(days=10)).isoformat()
    signal_rows = _get(
        s,
        f"{base}/option_flow_symbol_signals",
        {"select": "ticker,signal_date,net_directional_premium,unusual_premium,directional_confidence,net_bias_z",
         "signal_date": f"gte.{since}", "order": "signal_date.desc", "limit": "5000"},
    ).json()
    dp_dates = _get(s, f"{base}/darkpool_symbol_daily", {"select": "fetch_date", "fetch_date": f"lte.{report_date}", "order": "fetch_date.desc", "limit": "2000"}).json()
    uniq = sorted({str(r["fetch_date"])[:10] for r in dp_dates}, reverse=True)[:3]
    dp_summaries, darkpool_rows = [], []
    for i, d in enumerate(uniq):
        rows = _get(s, f"{base}/darkpool_symbol_daily", {"select": "symbol,fetch_date,net_premium,total_premium", "fetch_date": f"eq.{d}", "limit": "2000"}).json()
        dp_summaries.append({"fetch_date": d, "rows": len(rows), "net": sum(float(r.get("net_premium") or 0) for r in rows), "total": sum(float(r.get("total_premium") or 0) for r in rows)})
        if i == 0:
            darkpool_rows = rows
    return {
        "latest_dates": latest_dates,
        "verdict_rows": verdict_rows,
        "signal_rows": signal_rows,
        "darkpool_rows": darkpool_rows,
        "darkpool_summaries": dp_summaries,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _f(x: Any, fmt: str = "{:+.2f}") -> str:
    return "—" if x is None else fmt.format(x)


def render_markdown(res: dict[str, Any]) -> str:
    L: list[str] = []
    L.append(f"### 数据新鲜度 | Data freshness (as of {res['date']})")
    L.append("")
    L.append("| 表 Table | 最新 Latest | 滞后 Age (weekdays) | 状态 Status |")
    L.append("|---|---|---:|---|")
    for f in res["freshness"]:
        L.append(f"| `{f['table']}` | {f['latest'] or '—'} | {f['age_weekdays'] if f['age_weekdays'] is not None else '—'} | {f['status']} |")
    if res["darkpool_duplicates"]:
        L.append("")
        L.append("⚠ 暗池快照重复 Duplicate dark-pool snapshot: " + "; ".join(res["darkpool_duplicates"]) + "，该日暗池数据不可用 (treat as missing).")
    rows = res["reconciliation"]
    conf = [r for r in rows if r["status"] == "conflict"]
    ok = [r for r in rows if r["status"] == "confirmed"]
    L.append("")
    L.append(f"### 期权流对账 | Flow reconciliation ({len(ok)} confirmed · {len(conf)} conflict · {len(rows) - len(ok) - len(conf)} partial)")
    L.append("")
    if ok:
        bull = [r["symbol"] for r in ok if (r["flow_composite"] or 0) > 0]
        bear = [r["symbol"] for r in ok if (r["flow_composite"] or 0) < 0]
        L.append(f"- 三重确认 Triple-confirmed bullish: {', '.join(bull) or '—'}")
        L.append(f"- 三重确认 Triple-confirmed bearish: {', '.join(bear) or '—'}")
        L.append("")
    if conf:
        L.append("| 标的 | Verdict (quant) | Alerts total | Alert tilt | Premium today / 3d | net_bias_z | Dark pool | Composite | 冲突 Conflicts |")
        L.append("|---|---|---:|---:|---|---:|---:|---:|---|")
        for r in sorted(conf, key=lambda x: -abs(x["flow_composite"] or 0)):
            vq = f" ({r['verdict_quant']})" if r.get("verdict_quant") else ""
            dp = "—" if r["dp_net_mm"] is None else f"{r['dp_net_mm']:+,.0f}M"
            L.append(
                f"| **{r['symbol']}** | {r['verdict']}{vq} | {r['alert_count_total'] if r['alert_count_total'] is not None else '—'} "
                f"| {_f(r['alert_premium_tilt'])} | {_f(r['premium_today'])} / {_f(r['premium_3d'])} | {_f(r['net_bias_z'])} "
                f"| {dp} | {_f(r['flow_composite'], '{:+.0f}')} `{r['flow_label']}` | {', '.join(r['flow_conflicts'])} |"
            )
        L.append("")
        L.append("冲突标的：仓位减半或跳过，不得写成单一方向结论。Conflicted names: size down or skip; never present them as one-directional.")
    stale = [r["symbol"] for r in rows if r["verdict_stale"]]
    if stale:
        L.append("")
        L.append(f"⚠ 过期 verdict Stale verdicts (> {engine.VERDICT_STALE_DAYS}d, treated as no-data): {', '.join(stale)}")
    if res.get("board_issues") is not None:
        L.append("")
        L.append(f"### 简报标签核对 | Board label check ({len(res['board_issues'])} issues)")
        L.append("")
        for i in res["board_issues"]:
            if i["type"] == "false_staleness":
                L.append(f"- ❌ `{i['table']}` 被写成 stale，但最新 {i['latest']} — 删除该缺口条目")
            else:
                L.append(f"- ❌ **{i['symbol']}**: 简报写 `{i['claimed']}`，`flow_verdict_rows` 为 `{i['stored']}` — “{i['text']}”")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(inputs: dict[str, Any], report_date: str, board_text: str | None = None) -> dict[str, Any]:
    as_of = date.fromisoformat(report_date)
    fresh = freshness_report(inputs.get("latest_dates", {}), as_of)
    res: dict[str, Any] = {
        "date": report_date,
        "freshness": fresh,
        "darkpool_duplicates": darkpool_duplicate(inputs.get("darkpool_summaries", [])),
        "reconciliation": reconcile(inputs, as_of),
    }
    if board_text is not None:
        vmap = {str(r["symbol"]).upper(): str(r.get("verdict") or "") for r in inputs.get("verdict_rows", [])}
        res["board_issues"] = check_board(board_text, vmap, fresh)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Data freshness + flow reconciliation + board label check")
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", default=".")
    ap.add_argument("--inputs-json", help="Offline: pre-fetched inputs (fetch_inputs() shape)")
    ap.add_argument("--check-board", help="Briefing markdown/Notion text to lint against flow_verdict_rows")
    ap.add_argument("--strict", action="store_true", help="Exit 1 when the board check finds issues")
    a = ap.parse_args(argv)

    inputs = json.loads(Path(a.inputs_json).read_text(encoding="utf-8")) if a.inputs_json else fetch_inputs(a.date)
    board = Path(a.check_board).read_text(encoding="utf-8") if a.check_board else None
    res = run(inputs, a.date, board)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"flow_reconcile_{a.date}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md = render_markdown(res)
    (out / f"flow_reconcile_{a.date}.md").write_text(md, encoding="utf-8")
    print(md)
    if a.strict and res.get("board_issues"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
