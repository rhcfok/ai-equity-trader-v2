# -*- coding: utf-8 -*-
"""J Law v2 post-run QC audit for Supabase trade-jlaw-v2.

Audits what jlaw-v2-yahoo / jlaw-v2-chart wrote:
  1. coverage   — rows per run_date x jlaw_review_type vs watchlist
  2. labels     — jlaw_review_type outside {yahoo, chart}
  3. nulls      — nulls in score/prices/classification (Unreviewed exempt)
  4. sanity     — entry_price band, stop_loss band, stop_pct, rrr
  5. score-band — classification vs jlaw_score band consistency
  6. agreement  — cross-pipeline conflicts for same date+symbol

Read-only unless --normalize-labels (rewrites legacy 'data' labels to yahoo).

Usage:
    python jlaw_check.py [--date YYYY-MM-DD] [--json] [--normalize-labels]

Env: SUPABASE_URL, SUPABASE_KEY (fallbacks: supabase_url,
supabase_service_role), SUPABASE_V2_TABLE (default trade-jlaw-v2),
SUPABASE_WATCHLIST_TABLE (default watchlist). Stdlib only.
"""
import argparse, json, os, sys, urllib.request, urllib.error, urllib.parse

VALID_TYPES = {"yahoo", "chart"}
LEGACY_TYPES = {"data": "yahoo"}  # mislabel seen 2026-10-09/10, normalize to yahoo
CHART_CATEGORIES = ("core", "satellite", "watch1")
BANDS = [(15, 16, "A+"), (13, 14, "Valid"), (10, 12, "Watch"), (0, 9, "Skip")]


def env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


class Supa:
    def __init__(self):
        url = env("SUPABASE_URL", "supabase_url").rstrip("/")
        key = env("SUPABASE_KEY", "supabase_service_role")
        if not url or not key:
            print(json.dumps({"ok": False,
                              "error": "SUPABASE_URL / SUPABASE_KEY not set"}))
            sys.exit(2)
        if not url.endswith("/rest/v1"):
            url += "/rest/v1"
        self.url, self.key = url, key

    def get(self, table, params):
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{self.url}/{table}?{qs}",
            headers={"apikey": self.key,
                     "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(json.dumps({"ok": False,
                              "error": f"GET {table} HTTP {e.code}",
                              "detail": e.read().decode("utf-8", "ignore")[:400]}))
            sys.exit(2)

    def patch(self, table, params, body):
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{self.url}/{table}?{qs}", data=json.dumps(body).encode("utf-8"),
            method="PATCH",
            headers={"apikey": self.key,
                     "Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            return True
        except Exception as e:
            print(json.dumps({"ok": False,
                              "error": f"PATCH {table} failed: {e}"}))
            return False


def band_of(score):
    if score is None:
        return None
    for lo, hi, label in BANDS:
        if lo <= score <= hi:
            return label
    return None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def audit(supa, table, wl_table, date, normalize):
    findings = []

    # ---- labels (table-wide, not just this date) --------------------------
    rows = supa.get(table, {"select": "jlaw_review_type"})
    counts = {}
    for r in rows:
        t = r.get("jlaw_review_type")
        counts[t] = counts.get(t, 0) + 1
    bad_labels = {t: c for t, c in counts.items() if t not in VALID_TYPES}
    if bad_labels:
        sev = "warn" if normalize else "error"
        findings.append({
            "check": "labels", "severity": sev,
            "detail": f"non-standard jlaw_review_type values: {bad_labels}",
            "likely_cause": "runner wrote 'data' instead of the contract "
                            "value 'yahoo' — patch the runner default",
        })
        if normalize:
            for t in bad_labels:
                target = LEGACY_TYPES.get(t, "yahoo")
                ok = supa.patch(table, {"jlaw_review_type": f"eq.{t}"},
                                {"jlaw_review_type": target})
                findings[-1].setdefault("fixed", {})[t] = target if ok else "FAILED"

    # ---- rows for the audited date ---------------------------------------
    day = supa.get(table, {"select": "*", "run_date": f"eq.{date}"})
    by_type = {}
    for r in day:
        by_type.setdefault(r.get("jlaw_review_type"), []).append(r)

    wl = supa.get(wl_table, {"select": "symbol,category"})
    wl_all = {w["symbol"] for w in wl if w.get("symbol")}
    wl_chart = {w["symbol"] for w in wl
                if w.get("category") in CHART_CATEGORIES}

    # ---- coverage ---------------------------------------------------------
    coverage = {}
    for t, expected in (("yahoo", wl_all), ("chart", wl_chart)):
        got = {r["symbol"] for r in by_type.get(t, []) if r.get("symbol")}
        missing = sorted(expected - got)
        coverage[t] = {"rows": len(by_type.get(t, [])),
                       "expected_watchlist": len(expected),
                       "missing_count": len(missing),
                       "missing_sample": missing[:25]}
        if missing:
            findings.append({
                "check": "coverage", "severity": "error", "review_type": t,
                "detail": f"{len(missing)} watchlist symbols have no {t} row "
                          f"on {date}: {missing[:15]}",
                "likely_cause": f"partial {t} run — re-run the pipeline",
            })

    # ---- nulls ------------------------------------------------------------
    NULLABLE_IF_UNREVIEWED = {"jlaw_score", "entry_price", "stop_loss",
                              "target_price", "rrr", "stop_pct", "vcp_stage"}
    for t, rows_t in by_type.items():
        for col in ("jlaw_score", "entry_price", "current_price",
                    "stop_loss", "classification"):
            bad = [r["symbol"] for r in rows_t
                   if r.get(col) is None
                   and not (t == "chart"
                            and r.get("classification") == "Unreviewed"
                            and col in NULLABLE_IF_UNREVIEWED)]
            if bad:
                findings.append({
                    "check": "nulls", "severity": "error", "review_type": t,
                    "detail": f"{col} null for {len(bad)} {t} rows: "
                              f"{bad[:15]}",
                    "likely_cause": ("chart: review contract not filled — "
                                     "extend chart_upsert input / agent "
                                     "checklist" if t == "chart" else
                                     "yahoo: gate evaluator dropped a field "
                                     "— check jlaw_checklist.py output"),
                })

    # ---- sanity -----------------------------------------------------------
    for r in day:
        sym, t = r.get("symbol"), r.get("jlaw_review_type")
        cur, ent, stp = (num(r.get("current_price")),
                         num(r.get("entry_price")), num(r.get("stop_loss")))
        sp, rrr = num(r.get("stop_pct")), num(r.get("rrr"))
        if cur and ent and not (0.5 <= ent / cur <= 2.0):
            findings.append({"check": "sanity", "severity": "error",
                             "review_type": t, "symbol": sym,
                             "detail": f"entry_price {ent} implausible vs "
                                       f"current_price {cur}",
                             "likely_cause": "misplaced percentage/value "
                                             "into entry_price"})
        if cur and stp and stp > cur * 1.5:
            findings.append({"check": "sanity", "severity": "error",
                             "review_type": t, "symbol": sym,
                             "detail": f"stop_loss {stp} > 1.5x current "
                                       f"{cur}",
                             "likely_cause": "misplaced value into stop_loss"})
        if sp is not None and (sp < 0 or sp > 25):
            findings.append({"check": "sanity", "severity": "warn",
                             "review_type": t, "symbol": sym,
                             "detail": f"stop_pct {sp} outside [0, 25]",
                             "likely_cause": "sign convention or unit error"})
        if rrr is not None and rrr < 0:
            findings.append({"check": "sanity", "severity": "warn",
                             "review_type": t, "symbol": sym,
                             "detail": f"negative rrr {rrr}",
                             "likely_cause": "target below entry or stop "
                                             "above entry"})

    # ---- score-band consistency -------------------------------------------
    for r in day:
        score, cls = r.get("jlaw_score"), r.get("classification")
        if score is None or not cls or cls == "Unreviewed":
            continue
        band = band_of(score)
        if band and cls != band:
            findings.append({
                "check": "score_band", "severity": "warn",
                "review_type": r.get("jlaw_review_type"),
                "symbol": r.get("symbol"),
                "detail": f"classification '{cls}' but score {score} "
                          f"is band '{band}'",
                "likely_cause": "classification set by hand / hard-cap rule "
                                "— confirm it was intentional",
            })

    # ---- cross-pipeline agreement -----------------------------------------
    yahoo_map = {r["symbol"]: r for r in by_type.get("yahoo", [])}
    chart_map = {r["symbol"]: r for r in by_type.get("chart", [])}
    order = {"Skip": 0, "Watch": 1, "Valid": 2, "A+": 3}
    agreement = {"both": 0, "same_class": 0, "conflicts": []}
    for sym in sorted(set(yahoo_map) & set(chart_map)):
        y, c = yahoo_map[sym], chart_map[sym]
        yc, cc = y.get("classification"), c.get("classification")
        if not yc or not cc or cc == "Unreviewed":
            continue
        agreement["both"] += 1
        if yc == cc:
            agreement["same_class"] += 1
        elif abs(order.get(yc, 1) - order.get(cc, 1)) >= 2:
            agreement["conflicts"].append({
                "symbol": sym, "yahoo": yc, "chart": cc,
                "yahoo_score": y.get("jlaw_score"),
                "chart_score": c.get("jlaw_score"),
                "resolution": "chart overrides — treat as the stricter "
                              "verdict",
            })
    if agreement["conflicts"]:
        findings.append({
            "check": "agreement", "severity": "warn",
            "detail": f"{len(agreement['conflicts'])} symbols disagree by "
                      f"2+ classes between pipelines",
            "symbols": [c["symbol"] for c in agreement["conflicts"]],
        })

    ok = not any(f["severity"] == "error" for f in findings)
    return {"ok": ok, "run_date": date, "coverage": coverage,
            "label_counts": counts, "agreement": agreement,
            "findings": findings}


def latest_date(supa, table):
    rows = supa.get(table, {"select": "run_date", "order": "run_date.desc",
                            "limit": 1})
    return rows[0]["run_date"] if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="run_date to audit (default: latest)")
    ap.add_argument("--json", action="store_true",
                    help="print raw JSON only")
    ap.add_argument("--normalize-labels", action="store_true",
                    help="rewrite legacy 'data' jlaw_review_type to 'yahoo'")
    args = ap.parse_args()

    supa = Supa()
    table = env("SUPABASE_V2_TABLE") or "trade-jlaw-v2"
    wl_table = env("SUPABASE_WATCHLIST_TABLE") or "watchlist"
    date = args.date or latest_date(supa, table)
    if not date:
        print(json.dumps({"ok": False, "error": "no rows in table"}))
        sys.exit(2)

    rep = audit(supa, table, wl_table, date, args.normalize_labels)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"jlaw-v2-check  run_date={rep['run_date']}  "
              f"{'OK' if rep['ok'] else 'DEFECTS FOUND'}")
        for t, cov in rep["coverage"].items():
            print(f"  [{t}] {cov['rows']} rows / watchlist "
                  f"{cov['expected_watchlist']} "
                  f"(missing {cov['missing_count']})")
        ag = rep["agreement"]
        print(f"  agreement: {ag['same_class']}/{ag['both']} same class, "
              f"{len(ag['conflicts'])} hard conflicts")
        for f in rep["findings"]:
            sym = f" [{f.get('symbol')}]" if f.get("symbol") else ""
            print(f"  {f['severity'].upper():5s} {f['check']}{sym}: "
                  f"{f['detail']}")
    sys.exit(0 if rep["ok"] else 1)


if __name__ == "__main__":
    main()
