# -*- coding: utf-8 -*-
"""Upsert J Law chart-review results into Supabase trade-jlaw-v2.

Every reviewed symbol is recorded (including Skips) with
jlaw_review_type="chart", so chart rows coexist with the quantitative
"data" rows for the same run_date + symbol.

Usage:
    python chart_upsert.py --input chart_review_2026-10-09.json
    python chart_upsert.py --input reviews.json --run-date 2026-10-09

Input JSON: either a list of review objects, or
{"run_date": "YYYY-MM-DD", "regime": "Risk-On", "reviews": [...]}.
Each review object follows the jlaw-v2-chart output contract:
    {symbol, live_price, day_change_pct, structure, ma_notes, adx,
     macd_note, rsi, pivot, chart_stop, stop_pct, target_price, rrr,
     vcp_stage, chart_score, chart_score_breakdown, earnings_flag,
     action, reason, failed_items?}

stop_pct is normalized to a **positive distance in %** regardless of the
sign convention the reviewer used. chart_score (0–16, visual A/B/C/D
rubric) is stored in the table's `jlaw_score` column so both pipelines are
comparable in one column; distinguish them by jlaw_review_type.

Validation guards (bad values are nulled, flagged in failed_gates and
reported as validation_warnings, never silently written):
- pivot must be within [0.5x, 2x] of live_price (catches
  misplaced-percentage pivots like 10.4 meaning "10.4% below")
- chart_stop must not exceed 1.5x live_price

Env: SUPABASE_URL, SUPABASE_KEY, SUPABASE_V2_TABLE (default trade-jlaw-v2).
Stdlib only.
"""
import argparse, json, os, sys, urllib.request, urllib.error

REVIEW_TYPE = "chart"


def load_doc(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, list):
        return {"run_date": None, "regime": None, "reviews": d}
    return d


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def to_row(r, run_date, regime):
    action = (r.get("action") or "").strip()
    # table `classification` mirrors the chart action label
    classification = {
        "valid candidate": "Valid",
        "valid": "Valid",
        "watch": "Watch",
        "skip": "Skip",
        "unreviewed": "Unreviewed",
    }.get(action.lower(), action or None)
    stop_pct = _num(r.get("stop_pct"))
    if stop_pct is not None:
        stop_pct = round(abs(stop_pct), 2)  # normalize to positive distance %

    warnings = []
    live, pivot = _num(r.get("live_price")), _num(r.get("pivot"))
    # Guard: pivot must be a plausible price vs live_price (catches the
    # misplaced-percentage bug, e.g. pivot=10.4 meaning "10.4% below").
    if live and pivot and not (0.5 <= pivot / live <= 2.0):
        warnings.append(f"suspect_pivot:{pivot} vs live {live} — nulled")
        pivot = None
    stop = _num(r.get("chart_stop"))
    if live and stop and stop > live * 1.5:
        warnings.append(f"suspect_stop:{stop} above live {live} — nulled")
        stop = None

    failed = list(r.get("failed_items") or []) + [w.split(":")[0] for w in warnings]
    return {
        "run_date": run_date,
        "jlaw_review_type": REVIEW_TYPE,
        "regime": regime,
        "symbol": r.get("symbol"),
        "classification": classification,
        "jlaw_score": r.get("chart_score"),
        "current_price": r.get("live_price"),
        "entry_price": pivot,
        "stop_loss": stop,
        "target_price": r.get("target_price"),
        "rrr": r.get("rrr"),
        "stop_pct": stop_pct,
        "vcp_stage": r.get("vcp_stage"),
        "gates": {
            "structure": r.get("structure"),
            "ma_notes": r.get("ma_notes"),
            "adx": r.get("adx"),
            "macd_note": r.get("macd_note"),
            "rsi": r.get("rsi"),
            "day_change_pct": r.get("day_change_pct"),
            "rrr_note": r.get("rrr_note"),
            "chart_score_breakdown": r.get("chart_score_breakdown"),
            "earnings_flag": r.get("earnings_flag"),
            "reason": r.get("reason"),
            "validation_warnings": warnings or None,
        },
        "failed_gates": failed,
        "unknown_gates": [],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--run-date", help="override run_date (YYYY-MM-DD)")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    table = os.environ.get("SUPABASE_V2_TABLE", "trade-jlaw-v2")
    if not url or not key:
        print(json.dumps({"ok": False,
                          "error": "SUPABASE_URL / SUPABASE_KEY not set"}))
        sys.exit(2)
    if not url.endswith("/rest/v1"):
        url += "/rest/v1"

    doc = load_doc(args.input)
    run_date = args.run_date or doc.get("run_date")
    regime = doc.get("regime")
    if not run_date:
        print(json.dumps({"ok": False, "error": "run_date missing"}))
        sys.exit(2)

    rows = [to_row(r, run_date, regime) for r in doc.get("reviews", [])]
    rows = [r for r in rows if r["symbol"]]
    warned = [(r["symbol"], r["gates"]["validation_warnings"])
              for r in rows if r["gates"].get("validation_warnings")]
    if warned:
        print(json.dumps({"validation_warnings": warned}, ensure_ascii=False))
    if not rows:
        print(json.dumps({"ok": True, "upserted": 0}))
        return

    req = urllib.request.Request(
        f"{url}/{table}?on_conflict=run_date,symbol,jlaw_review_type",
        data=json.dumps(rows).encode("utf-8"),
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
        print(json.dumps({"ok": True, "upserted": len(rows),
                          "review_type": REVIEW_TYPE, "run_date": run_date}))
    except urllib.error.HTTPError as e:
        print(json.dumps({"ok": False, "error": f"supabase HTTP {e.code}",
                          "detail": e.read().decode("utf-8", "ignore")[:500]}))
        sys.exit(2)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"supabase upsert failed: {e}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
