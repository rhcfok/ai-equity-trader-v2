"""Fetch the latest quant2 leaderboard run from Supabase and emit the widget artifact.

Reads quant2_runs (latest run), quant2_leaderboard (per-ticker rows) via the
Supabase REST API. Credentials come from the automation input contract
(x-local / x-secret machine values). No third-party plugins are called.
"""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

# Optional .env fallback (keys: supabase_url, supabase_service_role).
# Override with the QUANT2_ENV_FILE environment variable; defaults to .env
# in the current working directory. No credentials belong in this file.
ENV_FALLBACK = Path(os.environ.get("QUANT2_ENV_FILE", ".env"))


def load_env_fallback():
    """Read supabase credentials from the project workspace .env if present."""
    url = key = ""
    try:
        for line in ENV_FALLBACK.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "supabase_url":
                url = v
            elif k == "supabase_service_role":
                key = v
    except OSError:
        pass
    return url, key


def rest_get(base, key, path, params):
    root = base if base.endswith("/rest/v1") else f"{base}/rest/v1"
    url = f"{root}/{path}?{urllib.parse.urlencode(params)}"
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
        raise RuntimeError(
            f"GET {url} -> HTTP {e.code}: {body} "
            f"(base_len={len(base)}, key_len={len(key)})"
        ) from e


def run(ctx):
    inp = ctx.get("input") or {}
    base = (inp.get("supabase_url") or "").rstrip("/")
    key = inp.get("supabase_key") or ""
    if not key:
        env_url, env_key = load_env_fallback()
        base = base or env_url.rstrip("/")
        key = key or env_key
    if not base or not key:
        raise RuntimeError("missing supabase_url / supabase_key in automation input")

    # latest run by run_ts
    runs = rest_get(base, key, "quant2_runs", {
        "select": "run_name,run_ts,validation_overall,probability_status",
        "order": "run_ts.desc",
        "limit": "1",
    })
    if not runs:
        raise RuntimeError("no rows in quant2_runs")
    run_row = runs[0]
    run_name = run_row["run_name"]

    rows = rest_get(base, key, "quant2_leaderboard", {
        "select": "rank,ticker,grp,as_of,score_hist_freq,n_active_rules,active_rules,rsi14,f_rsi_oversold",
        "run_name": f"eq.{run_name}",
        "order": "rank.asc",
    })
    if not rows:
        raise RuntimeError(f"no leaderboard rows for {run_name}")

    out_rows = []
    for r in rows:
        active = [s for s in (r.get("active_rules") or "").split(";") if s]
        out_rows.append({
            "rank": r["rank"],
            "ticker": r["ticker"],
            "grp": r.get("grp"),
            "score": float(r["score_hist_freq"]),
            "rsi14": float(r["rsi14"]) if r.get("rsi14") is not None else None,
            "n_active": r.get("n_active_rules") or 0,
            "oversold": bool(r.get("f_rsi_oversold")),
            "active_rules": active,
        })

    # group summary
    groups = {}
    for r in out_rows:
        g = groups.setdefault(r["grp"] or "unknown", {
            "grp": r["grp"] or "unknown", "n": 0, "score_sum": 0.0,
            "rsi_sum": 0.0, "rsi_n": 0, "active_sum": 0, "oversold": 0, "trend_aligned": 0,
        })
        g["n"] += 1
        g["score_sum"] += r["score"]
        if r["rsi14"] is not None:
            g["rsi_sum"] += r["rsi14"]
            g["rsi_n"] += 1
        g["active_sum"] += r["n_active"]
        if r["oversold"]:
            g["oversold"] += 1
        if "trend_alignment" in r["active_rules"]:
            g["trend_aligned"] += 1
    group_rows = [{
        "grp": g["grp"],
        "n": g["n"],
        "avg_score": round(g["score_sum"] / g["n"], 6),
        "avg_rsi": round(g["rsi_sum"] / g["rsi_n"], 2) if g["rsi_n"] else None,
        "avg_active": round(g["active_sum"] / g["n"], 2),
        "oversold": g["oversold"],
        "trend_aligned": g["trend_aligned"],
    } for g in groups.values()]
    group_rows.sort(key=lambda g: -g["avg_score"])

    as_of = rows[0].get("as_of") or ""
    artifact = {
        "run_name": run_name,
        "as_of": as_of,
        "generated_utc": (run_row.get("run_ts") or "")[:19].replace("T", " "),
        "validation_overall": run_row.get("validation_overall") or "",
        "probability_status": run_row.get("probability_status") or "",
        "n_tickers": len(out_rows),
        "groups": group_rows,
        "rows": out_rows,
    }
    return {"artifact": artifact}
