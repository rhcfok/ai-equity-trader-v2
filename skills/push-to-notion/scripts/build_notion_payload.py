"""Build a Notion-page payload (JSON on stdout) from Supabase trading data.

Surfaces:
  gex     -> latest gex_snapshot row per symbol (unsigned GEX magnitudes)
  quant2  -> latest quant2_runs run + quant2_leaderboard rows (Top-25 + groups)

The script only READS Supabase (PostgREST, stdlib urllib). The agent publishes
the returned payload to Notion via the Notion plugin (OAuth); no Notion token
belongs here. Credentials: env SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY,
fallback a .env file (keys supabase_url / supabase_service_role, either case).

Usage:
  python build_notion_payload.py gex    [--date YYYY-MM-DD] [--max-rows 50]
  python build_notion_payload.py quant2 [--date YYYY-MM-DD] [--max-rows 25]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SURFACES = {
    "gex": {
        "parent_path": "ai-trader-v2-kimi/gex",
        "title_fmt": "GEX Snapshot — {date}",
        "icon": "📊",
    },
    "quant2": {
        "parent_path": "ai-trader-v2-kimi/quant2",
        "title_fmt": "quant2 Leaderboard — {date}",
        "icon": "📈",
    },
}

STALE_HOURS = 36  # warn if newest gex snapshot is older than this


def log(msg):
    print(msg, file=sys.stderr)


def load_credentials(env_file):
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    path = Path(env_file)
    if (not url or not key) and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().lower(), v.strip().strip('"').strip("'")
            if k == "supabase_url" and not url:
                url = v
            elif k == "supabase_service_role" and not key:
                key = v
    return url.rstrip("/"), key


def rest_get(base, key, table, params, retries=2):
    root = base if base.endswith("/rest/v1") else f"{base}/rest/v1"
    url = f"{root}/{table}?{urllib.parse.urlencode(params)}"
    last_exc = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"GET {table} -> HTTP {e.code}: {body}") from e
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                log(f"GET {table}: attempt {attempt + 1} failed ({e}); retrying...")
                continue
    raise RuntimeError(f"GET {table} failed after {retries + 1} attempts: {last_exc}")


def fnum(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cell(v, fmt="{:,.2f}", prefix="", suffix=""):
    n = fnum(v)
    if n is None:
        return "—"
    return prefix + fmt.format(n) + suffix


def md_escape(s):
    return str(s).replace("|", "/").replace("\n", " ")


def parse_ts(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


# ---------------------------------------------------------------- gex surface

def build_gex(base, key, max_rows):
    rows = rest_get(base, key, "gex_snapshot", {
        "select": ("symbol,snapshot_ts,spot_price,net_gex_mm,abs_gex_mm,"
                   "call_wall,call_wall_gex_mm,put_wall,put_wall_gex_mm,"
                   "regime,gravity,near_term_gex_pct"),
        "order": "snapshot_ts.desc",
        "limit": "2000",
    })
    if not rows:
        raise RuntimeError("no rows in gex_snapshot")

    latest = {}
    for r in rows:  # first occurrence per symbol = latest (ordered desc)
        sym = r.get("symbol")
        if sym and sym not in latest:
            latest[sym] = r
    snaps = sorted(
        latest.values(),
        key=lambda r: (fnum(r.get("abs_gex_mm")) is None,
                       -(fnum(r.get("abs_gex_mm")) or 0.0)),
    )

    warnings = []
    ts_vals = [parse_ts(r["snapshot_ts"]) for r in snaps if r.get("snapshot_ts")]
    ts_min = min(ts_vals).strftime("%Y-%m-%d %H:%M") if ts_vals else "?"
    ts_max = max(ts_vals).strftime("%Y-%m-%d %H:%M") if ts_vals else "?"
    if ts_vals and max(ts_vals) < datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS):
        warnings.append(f"newest snapshot is {ts_max} UTC (>{STALE_HOURS}h old)")

    total_abs = sum(fnum(r.get("abs_gex_mm")) or 0.0 for r in snaps)
    shown = snaps if max_rows <= 0 else snaps[:max_rows]

    lines = [
        (f"Unsigned GEX snapshot — {len(snaps)} symbols, snapshots {ts_min} → {ts_max} UTC. "
         f"Total abs GEX ≈ ${total_abs:,.1f}MM. All call/put measures are unsigned "
         f"magnitudes (open interest is sign-neutral); no dealer positioning inferred."),
        "",
        f"Top {len(shown)} of {len(snaps)} by abs GEX:" if len(shown) < len(snaps)
        else f"All {len(snaps)} symbols, sorted by abs GEX:",
        "",
        "| # | Symbol | Spot | Net GEX ($MM) | Abs GEX ($MM) | Call wall | Put wall | Regime | Gravity |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(shown, 1):
        cw = r.get("call_wall")
        pw = r.get("put_wall")
        cw_s = f"{cell(cw, '{:,.4g}')} ({cell(r.get('call_wall_gex_mm'))})" if cw else "n/a"
        pw_s = f"{cell(pw, '{:,.4g}')} ({cell(r.get('put_wall_gex_mm'))})" if pw else "n/a"
        lines.append(
            f"| {i} | {md_escape(r.get('symbol'))} | {cell(r.get('spot_price'), '{:,.2f}', '$')} "
            f"| {cell(r.get('net_gex_mm'), '{:+,.2f}')} | {cell(r.get('abs_gex_mm'))} "
            f"| {cw_s} | {pw_s} | {md_escape(r.get('regime') or '—')} "
            f"| {md_escape(r.get('gravity') or '—')} |"
        )
    return snaps, "\n".join(lines), warnings


# -------------------------------------------------------------- quant2 surface

def build_quant2(base, key, max_rows):
    runs = rest_get(base, key, "quant2_runs", {
        "select": "run_name,run_ts,validation_overall,probability_status",
        "order": "run_ts.desc",
        "limit": "1",
    })
    if not runs:
        raise RuntimeError("no rows in quant2_runs")
    run = runs[0]

    rows = rest_get(base, key, "quant2_leaderboard", {
        "select": ("rank,ticker,grp,as_of,score_hist_freq,n_active_rules,"
                   "active_rules,rsi14,f_rsi_oversold"),
        "run_name": f"eq.{run['run_name']}",
        "order": "rank.asc",
    })
    if not rows:
        raise RuntimeError(f"no leaderboard rows for {run['run_name']}")

    shown = rows if max_rows <= 0 else rows[:max_rows]
    as_of = rows[0].get("as_of") or "?"

    lines = [
        (f"Run `{run['run_name']}` — data as of {as_of}. "
         f"Validation: {run.get('validation_overall') or '?'}; "
         f"probability_status: {run.get('probability_status') or '?'}. "
         f"{len(rows)} tickers. Score is a historical frequency, "
         f"not a calibrated probability."),
        "",
        f"## Top {len(shown)}",
        "",
        "| Rank | Ticker | Group | Score (hist. freq) | RSI14 | #Rules | Active rules |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in shown:
        active = md_escape(r.get("active_rules") or "")
        lines.append(
            f"| {r.get('rank')} | {md_escape(r.get('ticker'))} "
            f"| {md_escape(r.get('grp') or '—')} "
            f"| {cell(r.get('score_hist_freq'), '{:.4f}')} "
            f"| {cell(r.get('rsi14'), '{:.1f}')} "
            f"| {r.get('n_active_rules') or 0} | {active} |"
        )

    groups = {}
    for r in rows:
        g = groups.setdefault(r.get("grp") or "unknown",
                              {"n": 0, "score": 0.0, "rsi": [], "oversold": 0, "trend": 0})
        g["n"] += 1
        g["score"] += fnum(r.get("score_hist_freq")) or 0.0
        rsi = fnum(r.get("rsi14"))
        if rsi is not None:
            g["rsi"].append(rsi)
        if r.get("f_rsi_oversold"):
            g["oversold"] += 1
        if "trend_alignment" in (r.get("active_rules") or ""):
            g["trend"] += 1
    g_rows = sorted(groups.items(), key=lambda kv: -(kv[1]["score"] / kv[1]["n"]))

    lines += [
        "",
        "## Group summary",
        "",
        "| Group | Tickers | Avg score | Avg RSI14 | Oversold | Trend-aligned |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, g in g_rows:
        avg_rsi = f"{sum(g['rsi']) / len(g['rsi']):.1f}" if g["rsi"] else "—"
        lines.append(
            f"| {md_escape(name)} | {g['n']} | {g['score'] / g['n']:.4f} "
            f"| {avg_rsi} | {g['oversold']} | {g['trend']} |"
        )
    return rows, "\n".join(lines), []


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("surface", choices=sorted(SURFACES))
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="publish date for the page title (default: local today)")
    ap.add_argument("--env-file", default=os.environ.get("SUPABASE_ENV_FILE", ".env"),
                    help="fallback .env path (default: .env in cwd)")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="table row cap; 0 = all (default: 50 for gex, 25 for quant2)")
    args = ap.parse_args()
    max_rows = args.max_rows
    if max_rows is None:
        max_rows = 50 if args.surface == "gex" else 25

    base, key = load_credentials(args.env_file)
    if not base or not key:
        raise RuntimeError("missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY "
                           "(env or .env fallback)")

    if args.surface == "gex":
        rows, markdown, warnings = build_gex(base, key, max_rows)
    else:
        rows, markdown, warnings = build_quant2(base, key, max_rows)

    meta = SURFACES[args.surface]
    payload = {
        "surface": args.surface,
        "parent_path": meta["parent_path"],
        "title": meta["title_fmt"].format(date=args.date),
        "icon": meta["icon"],
        "publish_date": args.date,
        "row_count": len(rows),
        "warnings": warnings,
        "markdown": markdown,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()  # trailing newline
    log(f"OK: {payload['title']} — {len(rows)} rows, {len(markdown)} chars of markdown")


if __name__ == "__main__":
    main()
