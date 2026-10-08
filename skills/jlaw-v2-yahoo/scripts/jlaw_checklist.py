#!/usr/bin/env python3
"""jlaw_checklist.py — deterministic J Law doctrine gate evaluator.

Two modes:

  screen    Evaluate jlaw-yahoo-runner JSON results against J Law's
            objectively computable buy gates; classify each symbol as
            A+ setup / Valid / Watch / Skip.

  holdings  Evaluate a holdings CSV against J Law's sell/position rules.

Stdlib only. Thresholds configurable via JLAW_* env vars (see SKILL.md).
Research and analysis only — not personalized financial advice.
"""
import argparse
import csv
import json
import os
import sys
import urllib.request


def _f(env, default):
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


CFG = {
    "min_score": _f("JLAW_MIN_SCORE", 13),
    "max_chase_pct": _f("JLAW_MAX_CHASE_PCT", 5),
    "max_stop_pct": _f("JLAW_MAX_STOP_PCT", 8),
    "min_rrr": _f("JLAW_MIN_RRR", 2.0),
    "max_above_50ma_pct": _f("JLAW_MAX_ABOVE_50MA_PCT", 8),
    "max_base_range_pct": _f("JLAW_MAX_BASE_RANGE_PCT", 15),
    "max_off_high_pct": _f("JLAW_MAX_OFF_HIGH_PCT", 25),
    "regime": os.environ.get("JLAW_REGIME", "Neutral"),
}

MANUAL_CHECKLIST = [
    "Pattern quality: perfect base, tight pivot area (<=10% range), volume dry-up during pivot",
    "Pivot/pocket pivot has run >= 3 days",
    "RS line at/near new high; stock among strongest leaders of a strong sector",
    "Fundamentals support (earnings/sales acceleration; company in growth stage)",
    "No earnings release imminent (never carry a large position through earnings)",
    "Not a penny / operator-controlled stock; adequate liquidity",
    "Market regime: easy-money vs blood-sweat; if long rally, stay alert",
    "'If I could only buy 10 stocks this year, would this be one?'",
    "Stop-loss order will be submitted with/after the buy order",
]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def eval_gates(r):
    """Return dict of gate results for one runner result row."""
    price = _num(r.get("current_price"))
    entry = _num(r.get("entry_price"))
    stop = _num(r.get("stop_loss"))
    target = _num(r.get("target_price"))
    score = _num(r.get("jlaw_score"))
    m = r.get("metrics") or {}
    sma50 = _num(m.get("sma50"))
    sma200 = _num(m.get("sma200"))
    base_range = _num(m.get("base_range_pct"))
    off_high = _num(r.get("pct_from_52w_high"))  # negative when below high

    g = {}

    g["score_band"] = None if score is None else score >= CFG["min_score"]

    if price is not None and entry:
        chase = (price - entry) / entry * 100
        g["not_chasing"] = chase <= CFG["max_chase_pct"]
        g["chase_pct"] = round(chase, 2)
        g["within_2pct_of_pivot"] = chase <= 2
    else:
        g["not_chasing"] = None

    if entry and stop is not None and entry > 0:
        stop_pct = (entry - stop) / entry * 100
        g["stop_distance_ok"] = 0 < stop_pct <= CFG["max_stop_pct"]
        g["stop_pct"] = round(stop_pct, 2)
    else:
        g["stop_distance_ok"] = None

    if entry and stop is not None and target is not None and entry > stop:
        rrr = (target - entry) / (entry - stop)
        g["rrr_ok"] = rrr >= CFG["min_rrr"]
        g["rrr"] = round(rrr, 2)
    else:
        g["rrr_ok"] = None
        g["rrr"] = None

    if price is not None and sma50 and sma200:
        g["trend_ok"] = bool(r.get("above_50ma")) and price > sma200
        g["pct_above_50ma"] = round((price - sma50) / sma50 * 100, 2)
        g["not_extended_50ma"] = g["pct_above_50ma"] <= CFG["max_above_50ma_pct"]
    else:
        g["trend_ok"] = None
        g["not_extended_50ma"] = None

    if base_range is not None:
        g["base_tight"] = base_range <= CFG["max_base_range_pct"]
    else:
        g["base_tight"] = None

    if off_high is not None:
        g["near_52w_high"] = abs(off_high) <= CFG["max_off_high_pct"]
    else:
        g["near_52w_high"] = None

    vcp = (r.get("vcp_stage") or "").lower()
    g["vcp_stage"] = r.get("vcp_stage")
    g["pocket_pivot"] = bool(r.get("pocket_pivot"))
    g["vcp_constructive"] = None if not vcp else vcp not in ("none", "broken", "failed")

    return g


def classify(score, gates):
    hard = ["score_band", "not_chasing", "stop_distance_ok", "rrr_ok",
            "trend_ok", "not_extended_50ma", "base_tight", "near_52w_high"]
    vals = {k: gates.get(k) for k in hard}
    unknown = [k for k, v in vals.items() if v is None]
    failed = [k for k, v in vals.items() if v is False]
    if unknown:
        return "Watch", failed, unknown
    if not failed:
        return ("A+ setup" if (score or 0) >= 15 else "Valid"), failed, unknown
    if len(failed) <= 2 and (score or 0) >= 10:
        return "Watch", failed, unknown
    return "Skip", failed, unknown


def upsert_supabase(doc):
    """Upsert screen candidates into the Supabase doctrine table.

    Env: SUPABASE_URL, SUPABASE_KEY, SUPABASE_V2_TABLE (default trade-jlaw-v2).
    Uses the PostgREST upsert with the unique (run_date, symbol) constraint —
    same-date reruns replace rows, never duplicate them.
    """
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    table = os.environ.get("SUPABASE_V2_TABLE", "trade-jlaw-v2")
    if not url or not key:
        print(json.dumps({"ok": False,
                          "error": "SUPABASE_URL / SUPABASE_KEY not set"}))
        return 2
    if not url.endswith("/rest/v1"):
        url = url + "/rest/v1"
    rows = []
    for c in doc["candidates"]:
        g = c.get("gates") or {}
        rows.append({
            "run_date": doc.get("run_date"),
            "regime": doc.get("regime"),
            "symbol": c.get("symbol"),
            "classification": c.get("classification"),
            "jlaw_score": c.get("jlaw_score"),
            "current_price": c.get("current_price"),
            "entry_price": c.get("entry_price"),
            "stop_loss": c.get("stop_loss"),
            "target_price": c.get("target_price"),
            "rrr": c.get("rrr"),
            "chase_pct": g.get("chase_pct"),
            "stop_pct": g.get("stop_pct"),
            "pct_above_50ma": g.get("pct_above_50ma"),
            "vcp_stage": g.get("vcp_stage"),
            "pocket_pivot": g.get("pocket_pivot"),
            "gates": g,
            "failed_gates": c.get("failed_gates") or [],
            "unknown_gates": c.get("unknown_gates") or [],
        })
    if not rows:
        return 0
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/{table}?on_conflict=run_date,symbol",
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return 0
    except urllib.error.HTTPError as e:
        print(json.dumps({"ok": False, "error": f"supabase HTTP {e.code}",
                          "detail": e.read().decode("utf-8", "ignore")[:500]}))
        return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"supabase upsert failed: {e}"}))
        return 2


def cmd_screen(args):
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results", data if isinstance(data, list) else [])
    out = []
    counts = {"A+ setup": 0, "Valid": 0, "Watch": 0, "Skip": 0}
    for r in results:
        if r.get("data_status") != "ok":
            continue
        gates = eval_gates(r)
        cls, failed, unknown = classify(_num(r.get("jlaw_score")), gates)
        counts[cls] += 1
        if cls == "Skip" and not args.all:
            continue
        out.append({
            "symbol": r.get("symbol"),
            "classification": cls,
            "jlaw_score": r.get("jlaw_score"),
            "current_price": r.get("current_price"),
            "entry_price": r.get("entry_price"),
            "stop_loss": r.get("stop_loss"),
            "target_price": r.get("target_price"),
            "rrr": gates.get("rrr"),
            "gates": {k: v for k, v in gates.items()},
            "failed_gates": failed,
            "unknown_gates": unknown,
        })
    order = {"A+ setup": 0, "Valid": 1, "Watch": 2, "Skip": 3}
    out.sort(key=lambda x: (order[x["classification"]], -(x["jlaw_score"] or 0)))
    doc = {
        "run_date": data.get("run_date") if isinstance(data, dict) else None,
        "regime": CFG["regime"],
        "regime_note": ("Risk-Off: capital preservation — do not promote new buys."
                        if CFG["regime"].lower() == "risk-off" else
                        "As of 1 Oct 2026 MYT weekly: Risk-On. Confirm latest stance."),
        "config": CFG,
        "summary": counts,
        "candidates": out,
        "manual_checklist": MANUAL_CHECKLIST,
    }
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    rc = 0
    if args.write_supabase:
        rc = upsert_supabase(doc)
        if rc != 0:
            return rc
    print(text if not args.output else
          json.dumps({"ok": True, "path": args.output, "summary": counts,
                      "regime": CFG["regime"],
                      "supabase_upserted": len(doc["candidates"]) if args.write_supabase else 0},
                     ensure_ascii=False))
    return 0


def cmd_holdings(args):
    if not os.path.exists(args.holdings):
        print(json.dumps({"ok": False, "error": f"holdings file not found: {args.holdings}"}))
        return 2
    quotes = {}
    if args.input and os.path.exists(args.input):
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
        for r in data.get("results", []):
            quotes[r.get("symbol")] = r
    rows = []
    with open(args.holdings, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            cost = _num(row.get("avg_cost"))
            stop = _num(row.get("stop_loss"))
            isl = _num(row.get("initial_stop")) or stop
            q = quotes.get(sym) or {}
            cur = _num(row.get("current_price"))
            if cur is None:
                cur = _num(q.get("current_price"))
            actions, notes = [], []
            if cur is None or cost is None:
                actions.append("UNKNOWN_PRICE")
            else:
                pnl = (cur - cost) / cost * 100
                notes.append(f"pnl_pct={pnl:.2f}")
                if stop is not None and cur <= stop:
                    actions.append("STOP_HIT")
                if pnl <= -CFG["max_stop_pct"]:
                    actions.append("CUT_LOSS_GT_8PCT")
                if isl is not None and cost > isl:
                    r_multiple = (cur - cost) / (cost - isl)
                    notes.append(f"R={r_multiple:.2f}")
                    if r_multiple >= 1 and (stop is None or stop < cost):
                        actions.append("RAISE_STOP_TO_BREAKEVEN")
                if 15 <= pnl:
                    actions.append("TRIM_20_33PCT")
                    if pnl >= 20:
                        notes.append("consider trimming up to 50%")
                if q:
                    if q.get("above_50ma") is False:
                        actions.append("WARNING_BELOW_50MA")
                    m = q.get("metrics") or {}
                    chg = _num(m.get("daily_change_pct"))
                    vol = _num(m.get("equity_volume"))
                    avgv = _num(m.get("average_volume_20d"))
                    if chg is not None and chg <= -3 and vol and avgv and vol >= 1.5 * avgv:
                        actions.append("WARNING_HEAVY_VOLUME_DROP")
                if not actions:
                    actions.append("HOLD")
            rows.append({"symbol": sym, "avg_cost": cost, "current_price": cur,
                         "stop_loss": stop, "actions": actions, "notes": notes})
    doc = {"regime": CFG["regime"], "positions": rows,
           "rules_reminder": ["Never average down",
                              "Never let a big profit turn into a loss",
                              "Move stop to breakeven ASAP",
                              "No large position through earnings"]}
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(json.dumps({"ok": True, "path": args.output, "positions": len(rows)}))
    else:
        print(text)
    return 0


def main():
    p = argparse.ArgumentParser(description="J Law doctrine gate evaluator")
    sub = p.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("screen")
    s.add_argument("--input", required=True, help="jlaw-yahoo-runner JSON output")
    s.add_argument("--output")
    s.add_argument("--all", action="store_true", help="include Skip rows")
    s.add_argument("--write-supabase", action="store_true",
                   help="upsert candidates to SUPABASE_V2_TABLE (default trade-jlaw-v2); requires SUPABASE_URL/SUPABASE_KEY")
    s.set_defaults(fn=cmd_screen)
    h = sub.add_parser("holdings")
    h.add_argument("--holdings", required=True,
                   help="CSV: symbol,avg_cost,shares,stop_loss,initial_stop[,current_price]")
    h.add_argument("--input", help="runner JSON for quotes/warnings")
    h.add_argument("--output")
    h.set_defaults(fn=cmd_holdings)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
