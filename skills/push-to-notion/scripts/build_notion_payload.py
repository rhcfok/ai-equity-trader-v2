"""Build a Notion-page payload (JSON on stdout) from Supabase trading data.

Surfaces:
  gex     -> "GEX Daily Briefing — <date> <HH:MM> HKT" page: A→Z GEX map +
             strategy guidance table matching the established Notion briefing
             format (watchlist categories from the `watchlist` table, strike
             ladders from gex_snapshot.gamma_ladder, credit-spread guidance).
  quant2  -> "quant2 Leaderboard — <date>" page matching the established
             Kimi-published format: summary line (as-of, holdout, validation,
             validated candidates, probability status), factual day notes,
             Top-25 table, group summary, research-only disclaimer.

The script only READS Supabase (PostgREST, stdlib urllib). The agent publishes
the returned payload to Notion via the Notion plugin (OAuth); no Notion token
belongs here. Credentials: env SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY,
fallback a .env file (keys supabase_url / supabase_service_role, either case).

Usage:
  python build_notion_payload.py gex    [--date YYYY-MM-DD] [--max-rows N]
  python build_notion_payload.py quant2 [--date YYYY-MM-DD] [--max-rows N]

For gex, --max-rows defaults to 0 (all watchlist symbols), matching the
established A→Z briefing. For quant2 it defaults to 25 (Top-25 table).
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
from zoneinfo import ZoneInfo

SURFACES = {
    "gex": {
        "parent_path": "ai-trader-v2-kimi/gex",
        "title_fmt": "GEX Daily Briefing — {date} {time} HKT",
        "icon": "📊",
    },
    "quant2": {
        "parent_path": "ai-trader-v2-kimi/quant2",
        "title_fmt": "quant2 Leaderboard — {date}",
        "icon": "📈",
    },
}

HKT = ZoneInfo("Asia/Hong_Kong")
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "watchlist")
WATCHLIST_CATEGORIES = ["core", "satellite", "watch1"]
GEX_RESOLUTION_MM = 1.0  # sub-resolution threshold, mirrors gex-briefing
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
# Formatting rules mirror the gex-briefing skill (AI trader/gex-briefing.skill)
# so pages match the established "GEX Daily Briefing" Notion format.

def fmt_net(net_mm):
    """Unsigned GEX magnitude display: $+9.96M when >= $1M, else $+440.0K."""
    sign = "+" if net_mm >= 0 else "-"
    abs_v = abs(net_mm)
    if abs_v >= 1.0:
        return f"${sign}{abs_v:.2f}M"
    return f"${sign}{abs_v * 1000:.1f}K"


def fmt_wall(v):
    if v is None:
        return "n/a"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(fv - round(fv)) < 1e-6:
        return f"${int(round(fv))}"
    return f"${fv:g}"


def parse_ladder(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        try:
            out.append({
                "side": e.get("side") or "",
                "gex_mm": float(e.get("gex_mm") or 0.0),
                "strike": float(e.get("strike")),
            })
        except (TypeError, ValueError):
            continue
    return out


def ladder_line(entries, n=4):
    parts = []
    for e in entries[:n]:
        s = e["strike"]
        s_txt = str(int(round(s))) if abs(s - round(s)) < 1e-6 else f"{s:g}"
        parts.append(f"{s_txt}({e['gex_mm']:+.3f})")
    return ", ".join(parts) if parts else "—"


def derive_validity(row, net_mm):
    validity = ["ok"]
    if row.get("call_wall") is None and row.get("put_wall") is None:
        validity.append("walls_undefined")
    if abs(net_mm) < GEX_RESOLUTION_MM or str(row.get("regime") or "") == "sub_resolution":
        if "sub_resolution" not in validity:
            validity.append("sub_resolution")
    return validity


def strike_buffer(spot):
    raw = max(1.0, round(spot * 0.005))
    if spot >= 500:
        return float(max(5, int(round(raw / 5.0) * 5)))
    return float(max(1, int(round(raw))))


def guidance(row, net_mm, validity):
    regime = str(row.get("regime") or "")
    gravity = str(row.get("gravity") or "n/a")
    cw = row.get("call_wall")
    pw = row.get("put_wall")
    spot = float(row.get("spot_price") or 0.0)

    map_only = (
        "sub_resolution" in validity
        or "walls_undefined" in validity
        or regime == "sub_resolution"
        or (gravity == "up" and cw is None)
        or (gravity == "down" and pw is None)
        or (gravity in ("n/a", "") and (cw is None or pw is None))
        or (regime == "positive" and cw is None)
        or (regime == "negative" and pw is None)
    )
    if map_only:
        return "Map-only: net GEX below $1M threshold or no wall on the correct side."

    buf = strike_buffer(spot)
    if regime == "positive" or (gravity == "up" and cw is not None):
        cw_f = float(cw)
        long_k = cw_f + buf
        cw_i = int(round(cw_f)) if abs(cw_f - round(cw_f)) < 1e-6 else cw_f
        lk_i = int(round(long_k)) if abs(long_k - round(long_k)) < 1e-6 else long_k
        return (f"Structure: short {cw_i} / long {lk_i} call credit spread, 0–14 DTE. "
                f"- Entry: take premium at {cw_i}. Target: full decay. "
                f"Invalidation: close >{cw_i}.")
    if regime == "negative" or (gravity == "down" and pw is not None):
        pw_f = float(pw)
        long_k = pw_f - buf
        pw_i = int(round(pw_f)) if abs(pw_f - round(pw_f)) < 1e-6 else pw_f
        lk_i = int(round(long_k)) if abs(long_k - round(long_k)) < 1e-6 else long_k
        return (f"Structure: short {pw_i} / long {lk_i} put credit spread, 0–14 DTE. "
                f"- Entry: take premium at {pw_i}. Target: full decay. "
                f"Invalidation: close <{pw_i}.")
    return "Map-only: net GEX below $1M threshold or no wall on the correct side."


def build_gex(base, key, max_rows):
    wl = rest_get(base, key, WATCHLIST_TABLE, {
        "select": "symbol,category",
        "category": f"in.({','.join(WATCHLIST_CATEGORIES)})",
        "limit": "1000",
    })
    cat = {str(r.get("symbol") or "").upper(): str(r.get("category") or "")
           for r in wl}
    if not cat:
        raise RuntimeError(f"no watchlist rows in {WATCHLIST_TABLE}")

    # Latest row per symbol. Pages through 1000-row PostgREST cap until every
    # watchlist symbol is found or the table is exhausted.
    latest = {}
    select = ("symbol,snapshot_ts,spot_price,net_gex_mm,regime,gravity,"
              "call_wall,put_wall,gamma_ladder")
    for offset in range(0, 6000, 1000):
        rows = rest_get(base, key, "gex_snapshot", {
            "select": select,
            "order": "snapshot_ts.desc",
            "offset": str(offset),
            "limit": "1000",
        })
        if not rows:
            break
        for r in rows:  # first occurrence per symbol = latest (ordered desc)
            sym = str(r.get("symbol") or "").upper()
            if sym and sym in cat and sym not in latest:
                latest[sym] = r
        if len(latest) >= len(cat) or len(rows) < 1000:
            break

    if not latest:
        raise RuntimeError("no rows in gex_snapshot")

    snaps = [latest[s] for s in sorted(latest)]
    missing = sorted(set(cat) - set(latest))

    warnings = []
    ts_vals = [parse_ts(r["snapshot_ts"]) for r in snaps if r.get("snapshot_ts")]
    if ts_vals and max(ts_vals) < datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS):
        warnings.append(f"newest snapshot is {max(ts_vals).strftime('%Y-%m-%d %H:%M')} UTC (>{STALE_HOURS}h old)")
    if missing:
        warnings.append(f"missing snapshots for {len(missing)} watchlist symbols: {', '.join(missing[:10])}")

    shown = snaps if max_rows <= 0 else snaps[:max_rows]
    lines = [
        (f"Source: latest gex_snapshot rows. {len(snaps)}/{len(cat)} symbols covered, "
         f"sorted A→Z with watchlist category. Methodology unchanged from 2026-08-02 "
         f"audit fix: nearest-DTE per strike, min-OI 50, moneyness ±20%, sub-resolution "
         f"threshold $1M, walls side-of-spot checked. **Signed dealer positioning NOT "
         f"computed** — output is unsigned call/put-side GEX magnitude."),
        "",
        "## GEX Map & Strategy Guidance",
        "",
        "| Symbol | Category | Regime | Net GEX (unsigned) | Spot | Gravity | Call wall | Put wall | Validity | Above spot | Below spot | Guidance |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in shown:
        sym = str(r.get("symbol") or "").upper()
        spot = float(r.get("spot_price") or 0.0)
        net_mm = float(r.get("net_gex_mm") or 0.0)
        validity = derive_validity(r, net_mm)
        ladder = parse_ladder(r.get("gamma_ladder"))
        above = sorted([e for e in ladder if e["side"] == "above"],
                       key=lambda e: -abs(e["gex_mm"]))
        below = sorted([e for e in ladder if e["side"] == "below"],
                       key=lambda e: -abs(e["gex_mm"]))
        lines.append(
            f"| {sym} | {md_escape(cat.get(sym) or '—')} | {md_escape(str(r.get('regime') or '—'))} "
            f"| {fmt_net(net_mm)} | ${spot:.2f} | {md_escape(str(r.get('gravity') or '—'))} "
            f"| {fmt_wall(r.get('call_wall'))} | {fmt_wall(r.get('put_wall'))} "
            f"| {', '.join(validity)} | {ladder_line(above)} | {ladder_line(below)} "
            f"| {md_escape(guidance(r, net_mm, validity))} |"
        )
    return snaps, "\n".join(lines), warnings


# -------------------------------------------------------------- quant2 surface

def build_quant2(base, key, max_rows):
    runs = rest_get(base, key, "quant2_runs", {
        "select": ("run_name,run_ts,validation_overall,probability_status,"
                   "holdout_start,holdout_end,validated_candidates,n_tickers"),
        "order": "run_ts.desc",
        "limit": "1",
    })
    if not runs:
        raise RuntimeError("no rows in quant2_runs")
    run = runs[0]

    rows = rest_get(base, key, "quant2_leaderboard", {
        "select": ("rank,ticker,grp,as_of,score_hist_freq,active_rules,rsi14,"
                   "f_rsi_oversold"),
        "run_name": f"eq.{run['run_name']}",
        "order": "rank.asc",
    })
    if not rows:
        raise RuntimeError(f"no leaderboard rows for {run['run_name']}")

    shown = rows if max_rows <= 0 else rows[:max_rows]
    as_of = rows[0].get("as_of") or "?"
    cands = run.get("validated_candidates")
    cand_txt = "NONE" if not cands else str(len(cands))

    lines = [
        (f"{len(rows)} tickers scored as of {as_of} · holdout "
         f"{run.get('holdout_start') or '?'} → {run.get('holdout_end') or '?'} · "
         f"validation **{run.get('validation_overall') or '?'}** · validated "
         f"candidates: **{cand_txt}** · probability status: "
         f"`{run.get('probability_status') or '?'}`"),
    ]

    # Factual day notes derived from the same rows (no editorial judgment).
    top = shown[0]
    t1, s1 = top.get("ticker"), fnum(top.get("score_hist_freq")) or 0.0
    if len(shown) > 1:
        t2, s2 = shown[1].get("ticker"), fnum(shown[1].get("score_hist_freq")) or 0.0
    else:
        t2, s2 = "—", 0.0
    grp_top = {}
    for r in shown:
        grp_top[r.get("grp") or "unknown"] = grp_top.get(r.get("grp") or "unknown", 0) + 1
    gname, gcount = max(grp_top.items(), key=lambda kv: kv[1])

    groups = {}
    for r in rows:
        g = groups.setdefault(r.get("grp") or "unknown",
                              {"n": 0, "s": 0.0, "rsi": [], "os": 0, "tr": 0})
        g["n"] += 1
        g["s"] += fnum(r.get("score_hist_freq")) or 0.0
        rsi = fnum(r.get("rsi14"))
        if rsi is not None:
            g["rsi"].append(rsi)
        if r.get("f_rsi_oversold"):
            g["os"] += 1
        if "trend_alignment" in (r.get("active_rules") or ""):
            g["tr"] += 1
    gtop = groups[gname]
    weakest = sorted(shown, key=lambda r: (fnum(r.get("rsi14")) is None,
                                           fnum(r.get("rsi14")) or 0.0))[:3]
    wk_txt = ", ".join(f"{w.get('ticker')} {fnum(w.get('rsi14')):.1f}"
                       for w in weakest if fnum(w.get("rsi14")) is not None)
    os_top = sum(1 for r in shown if r.get("f_rsi_oversold"))
    lines.append(
        f"**Day notes:** {t1} holds #1 ({s1:.4f}) with {t2} at #2 ({s2:.4f}). "
        f"{gname} dominates the top {len(shown)} — {gcount}/{len(shown)} names, "
        f"group avg score {gtop['s'] / gtop['n']:.4f}, {gtop['tr']}/{gtop['n']} "
        f"trend-aligned. {os_top} oversold (RSI<30) row{'s' if os_top != 1 else ''} "
        f"in the top {len(shown)}; weakest top-{len(shown)} RSI readings: "
        f"{wk_txt or '—'}."
    )

    heading = "## Top 25" if len(shown) == 25 else f"## Top {len(shown)}"
    lines += [
        "",
        heading,
        "",
        "| rank | ticker | group | score | RSI(14) | active rules |",
        "|---|---|---|---|---|---|",
    ]
    for r in shown:
        active = ", ".join(s for s in (r.get("active_rules") or "").split(";") if s)
        lines.append(
            f"| {r.get('rank')} | {md_escape(r.get('ticker'))} | {md_escape(r.get('grp') or '—')} "
            f"| {cell(r.get('score_hist_freq'), '{:.4f}')} | {cell(r.get('rsi14'), '{:.1f}')} "
            f"| {md_escape(active) or '—'} |"
        )

    g_rows = sorted(groups.items(), key=lambda kv: -(kv[1]["s"] / kv[1]["n"]))
    lines += [
        "",
        "## Group summary",
        "",
        "| group | tickers | avg score | avg RSI | oversold | trend-aligned |",
        "|---|---|---|---|---|---|",
    ]
    for name, g in g_rows:
        avg_rsi = f"{sum(g['rsi']) / len(g['rsi']):.1f}" if g["rsi"] else "—"
        lines.append(
            f"| {md_escape(name)} | {g['n']} | {g['s'] / g['n']:.4f} "
            f"| {avg_rsi} | {g['os']} | {g['tr']} |"
        )
    lines += [
        "",
        "---",
        "",
        "*Score is a historical frequency, not a calibrated probability. This is "
        "research and analysis only, not personalized financial advice.*",
    ]
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
                    help="table row cap; 0 = all (default: 0 for gex, 25 for quant2)")
    args = ap.parse_args()
    max_rows = args.max_rows
    if max_rows is None:
        max_rows = 0 if args.surface == "gex" else 25

    base, key = load_credentials(args.env_file)
    if not base or not key:
        raise RuntimeError("missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY "
                           "(env or .env fallback)")

    if args.surface == "gex":
        rows, markdown, warnings = build_gex(base, key, max_rows)
    else:
        rows, markdown, warnings = build_quant2(base, key, max_rows)

    meta = SURFACES[args.surface]
    title = meta["title_fmt"].format(
        date=args.date,
        time=datetime.now(HKT).strftime("%H:%M"),
    )
    payload = {
        "surface": args.surface,
        "parent_path": meta["parent_path"],
        "title": title,
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
