#!/usr/bin/env python3
"""Shared fetch-latest data module for short-date scripts (top10 / part41).

Fetches the LATEST of everything directly from Supabase (read-only GETs):
  - scored batch: latest report_date across davis/uw ledgers that HAS em-probability rows
    (if the newest batch is un-scored, falls back to the newest scored batch and flags it)
  - verdict date: latest flow-verdicts.verdict_date; per-symbol reconciliation via
    flow_reconcile.py subprocess (same skill dir)
  - stldc: latest run_date;  quant2: latest run_name;  darkpool: latest fetch_date with
    duplicate-vs-prior-day check;  normalized flow: latest signal_date
  - valuation: equity_valuation_uw_daily full columns, latest as_of per ticker (6mo staleness
    is applied downstream, not here)

Creds: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) env, else repo-root .env.
Writes snapshot JSONs into --indir for audit and offline re-runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]


def load_creds() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE")
           or os.getenv("SUPABASE_KEY"))
    if url and key:
        if not url.endswith("/rest/v1"):
            url = url.rstrip("/") + "/rest/v1"
        return url, key
    env = {}
    p = Path.cwd() / ".env"
    if not p.exists():
        p = REPO_ROOT / ".env"
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    url = env.get("supabase_url") or env.get("SUPABASE_URL") or ""
    key = (env.get("supabase_service_role") or env.get("SUPABASE_SERVICE_ROLE_KEY")
           or env.get("SUPABASE_SERVICE_ROLE") or env.get("SUPABASE_KEY") or "")
    if not url.endswith("/rest/v1"):
        url = url.rstrip("/") + "/rest/v1"
    return url, key


def sb_all(url: str, key: str, table: str, params: dict | None = None) -> list:
    import requests
    rows, off = [], 0
    while True:
        r = requests.get(f"{url}/{table}",
                         params=dict(params or {}, offset=off), timeout=120,
                         headers={"apikey": key, "Authorization": f"Bearer {key}"})
        r.raise_for_status()
        b = r.json()
        if not isinstance(b, list):
            raise RuntimeError(f"{table}: {str(b)[:200]}")
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return rows


def fetch_latest(indir: Path, creds: tuple[str, str] | None = None) -> dict:
    """Fetch latest-everything; persist snapshots to indir. Returns state dict."""
    url, key = creds or load_creds()
    indir.mkdir(parents=True, exist_ok=True)

    def dump(rows, name):
        (indir / name).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    def latest_scalar(table, col, extra=None):
        p = {"select": col, "order": f"{col}.desc", "limit": 3}
        if extra:
            p.update(extra)
        rows = sb_all(url, key, table, p)
        return sorted({str(x[col]) for x in rows}, reverse=True)

    # 1) ledger batch dates + em coverage
    davis_dates = latest_scalar("option-opportunity-davis", "report_date")
    uw_dates = latest_scalar("uw-option-opportunities", "report_date")
    em_dates = latest_scalar("em-probability", "report_date")
    batch = max([d for d in (set(davis_dates) | set(uw_dates)) if d in set(em_dates)], default=None)
    unscored = [d for d in (set(davis_dates) | set(uw_dates)) - set(em_dates)]
    if batch is None:
        raise SystemExit(f"no scored batch: ledger dates {sorted(set(davis_dates) | set(uw_dates))}, "
                         f"em dates {em_dates}; run skills/em-expected-move/scripts/em_score.py first")
    state = {"batch_date": batch, "unscored_batches": sorted(unscored, reverse=True),
             "em_dates": em_dates[:3]}

    # 2) ledger + em rows for the batch
    dump(sb_all(url, key, "option-opportunity-davis", {"select": "*", "report_date": f"eq.{batch}"}), "ledger_davis.json")
    dump(sb_all(url, key, "uw-option-opportunities", {"select": "*", "report_date": f"eq.{batch}"}), "ledger_uw.json")
    dump(sb_all(url, key, "em-probability", {"select": "*", "report_date": f"eq.{batch}"}), "em_scores.json")

    # 3) freshest verdict date + reconciliation via flow_reconcile.py subprocess
    vdate = latest_scalar("flow-verdicts", "verdict_date")[0]
    state["vdate"] = vdate
    rc = subprocess.run([sys.executable, str(SCRIPT_DIR / "flow_reconcile.py"),
                         "--date", vdate, "--out", str(indir)],
                        capture_output=True, text=True, timeout=300,
                        env=dict(os.environ,
                                 SUPABASE_URL=url.split("/rest/v1")[0],
                                 SUPABASE_SERVICE_ROLE_KEY=key))
    rcfile = indir / f"flow_reconcile_{vdate}.json"
    if not rcfile.exists():
        raise SystemExit(f"flow_reconcile failed: {rc.stdout[-300:]} {rc.stderr[-300:]}")

    # 4) stldc / quant2 / darkpool / normalized flow / valuation latest
    stldc_date = latest_scalar("trade-stldc-v2", "run_date")[0]
    state["stldc_date"] = stldc_date
    dump(sb_all(url, key, "trade-stldc-v2", {"select": "*", "run_date": f"eq.{stldc_date}"}), "stldc_latest.json")
    qrow = sb_all(url, key, "quant2_leaderboard", {"select": "run_name,as_of", "order": "created_at.desc", "limit": 1})
    qrun = qrow[0]["run_name"] if qrow else None
    state["quant2_run"] = qrun
    if qrun:
        dump(sb_all(url, key, "quant2_leaderboard", {"select": "*", "run_name": f"eq.{qrun}"}), "quant2_leaderboard.json")
    dp_dates = latest_scalar("darkpool_symbol_daily", "fetch_date")
    dp_date = dp_dates[0]
    dp_prev = dp_dates[1] if len(dp_dates) > 1 else None
    dp_rows = sb_all(url, key, "darkpool_symbol_daily",
                     {"select": "symbol,fetch_date,net_premium,buy_premium,sell_premium,total_premium,print_count,block_count,accumulating,top_level,top_level_premium,window_start,window_end",
                      "fetch_date": f"eq.{dp_date}"})
    dump(dp_rows, "darkpool_recent.json")
    if dp_prev:
        prev = sb_all(url, key, "darkpool_symbol_daily",
                      {"select": "symbol,net_premium", "fetch_date": f"eq.{dp_prev}"})
        same = {str(x.get("symbol")): round(n_(x.get("net_premium")), 2) for x in prev}
        dup = all(abs(same.get(str(x.get("symbol")), 1e18) - round(n_(x.get("net_premium")), 2)) < 0.01
                  for x in dp_rows if str(x.get("symbol")) in same)
        state["darkpool_duplicate_of_prior_day"] = bool(dup and len(prev) == len(dp_rows))
    state["dp_date"] = dp_date
    nf_date = latest_scalar("option_flow_symbol_signals", "signal_date")[0]
    state["nf_date"] = nf_date
    dump(sb_all(url, key, "option_flow_symbol_signals", {"select": "*", "signal_date": f"eq.{nf_date}"}), "normalized_flow_recent.json")
    sb_date = latest_scalar("watchlist-signal-board", "run_date")[0]
    state["signal_board_date"] = sb_date
    dump(sb_all(url, key, "watchlist-signal-board", {"select": "*", "run_date": f"eq.{sb_date}"}), "signal_board.json")
    val = sb_all(url, key, "equity_valuation_uw_daily",
                 {"select": "ticker,as_of_date,price,fair_value,fair_value_adj,buy_below,buy_below_adj,anchor_verdict,valuation_category,"
                            "reference_source,reference_valuation_date,reference_fair_value,reference_buy_below,reference_price_target,"
                            "reference_upside_pct,next_earnings_date,is_us_listed",
                  "order": "as_of_date.desc,ticker.asc"})
    dump(val, "valuation_full.json")
    state["val_as_of"] = sorted({str(x.get("as_of_date")) for x in val}, reverse=True)[:2]
    state["indir"] = str(indir)
    (indir / "fetch_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return state


def n_(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def run_em_score() -> str:
    """Re-score the latest batch with the freshest em-model-snapshot (writes em-probability)."""
    p = REPO_ROOT / "skills" / "em-expected-move" / "scripts" / "em_score.py"
    rc = subprocess.run([sys.executable, str(p)], capture_output=True, text=True, timeout=600)
    tail = (rc.stdout or "")[-400:] + (rc.stderr or "")[-200:]
    return f"exit {rc.returncode}: {tail}"
