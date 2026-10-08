#!/usr/bin/env python3
"""Upsert a completed quant2 (technical-direction-probability) run into Supabase.

Reads the run directory produced by run_direction_probability.py and pushes:
  quant2_runs               one row per run (metadata, gates verdict, checks)
  quant2_rule_scorecard     one row per tested rule/combo (23 for this config)
  quant2_leaderboard        one row per ticker (current rule states + baselines)
  quant2_mae_strike_bridge  MAE touch-probability samples

Tables must already exist (see quant2_schema.sql). Credentials come from the
local .env (supabase_url / supabase_service_role); no secrets are printed.

Usage:
  python quant2_upsert_supabase.py --run-dir quant2_repo/runs/run_20261002 [--dry-run]
"""
import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"

NUMERIC_COLS_SCORECARD = {
    "wf_n", "wf_dir_lift", "wf_ret_lift", "wf_worst5", "ho_win_rate",
    "ho_wilson_lo", "ho_wilson_hi", "ho_mean_ret", "ho_median_ret",
    "ho_mean_mae", "ho_mean_mfe", "ho_worst5", "dir_lift", "ret_lift",
    "tk_dir_delta", "tk_dir_ci_lo", "tk_dir_ci_hi", "tk_ret_delta",
    "tk_ret_ci_lo", "tk_ret_ci_hi", "mc_dir_diff", "mc_dir_ci_lo",
    "mc_dir_ci_hi", "p_raw", "q_bh", "p_holm", "loo_ticker_min",
    "loo_quarter_min", "loo_month_min",
}
INT_COLS_SCORECARD = {"ho_raw_days", "ho_n", "ho_tickers"}
BOOL_COLS_SCORECARD = {
    "is_combo", "g1_episodes", "g2_lift", "g3_ticker_adj", "g4_bootstrap_ci",
    "g5_subperiods", "g6_worst5", "g7_fdr", "g8_concentration", "passes_all",
}
TOUCH_RENAME = {f"touch_{p}%": f"touch_{p}pct" for p in (2, 3, 5, 7, 10, 13)}


def load_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def rest_base(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/rest/v1") else f"{base}/rest/v1"


def upsert(base: str, key: str, table: str, rows: list[dict], on_conflict: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[DRY RUN] {table}: would upsert {len(rows)} row(s) on ({on_conflict}); first row keys: {sorted(rows[0]) if rows else '-'}")
        return
    payload = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{base}/{table}?on_conflict={on_conflict}",
        data=payload,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"[OK] {table}: upserted {len(rows)} row(s) (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] {table}: HTTP {e.code}: {e.read().decode()[:800]}")
        raise SystemExit(1)


def to_num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def to_int(v):
    n = to_num(v)
    return int(n) if n is not None else None


def to_bool(v):
    if v in ("True", "true", True):
        return True
    if v in ("False", "false", False):
        return False
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    run_name = run_dir.name

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    meta = scorecard["meta"]
    checks = json.loads((run_dir / "integrity_checks.json").read_text())
    calib = json.loads((run_dir / "calibration_metrics.json").read_text())

    env = load_env()
    base = rest_base(env["supabase_url"])
    key = env["supabase_service_role"]

    # 1) run metadata
    run_row = {
        "run_name": run_name,
        "run_ts": checks.get("run_utc", "").replace(" ", "T") + "Z" or None,
        "config_sha256": meta.get("config_sha256"),
        "registry_sha256": (checks.get("registry_frozen_hash", {}).get("detail", "").replace("match ", "") or None),
        "eval_first": meta.get("eval_first"),
        "eval_last": meta.get("eval_last"),
        "dev_start": meta.get("dev", [None, None])[0],
        "dev_end": meta.get("dev", [None, None])[1],
        "holdout_start": meta.get("holdout", [None, None])[0],
        "holdout_end": meta.get("holdout", [None, None])[1],
        "n_tickers": len(json.loads((run_dir / "data" / "data_manifest.json").read_text()).get("tickers", {})) if (run_dir / "data" / "data_manifest.json").exists() else None,
        "n_obs": meta.get("n_obs"),
        "history_years": meta.get("history_years"),
        "validated_candidates": meta.get("validated_candidates") or [],
        "probability_status": calib.get("probability_status"),
        "calibration_pass": calib.get("calibration_pass"),
        "validation_overall": "PASS" if checks.get("overall_ok") else "FAIL",
        "checks": {k: (v.get("ok") if isinstance(v, dict) else v) for k, v in checks.items()},
    }
    upsert(base, key, "quant2_runs", [run_row], "run_name", args.dry_run)

    # 2) rule scorecard
    with open(run_dir / "scorecard.csv", newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            row = {"run_name": run_name}
            for col, val in r.items():
                if col in NUMERIC_COLS_SCORECARD:
                    row[col] = to_num(val)
                elif col in INT_COLS_SCORECARD:
                    row[col] = to_int(val)
                elif col in BOOL_COLS_SCORECARD:
                    row[col] = to_bool(val)
                else:
                    row[col] = val or None
            rows.append(row)
    upsert(base, key, "quant2_rule_scorecard", rows, "run_name,rule", args.dry_run)

    # 3) leaderboard
    lb_path = run_dir / "leaderboard" / "leaderboard_all.csv"
    with open(lb_path, newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            row = {"run_name": run_name}
            for col, val in r.items():
                target = "grp" if col == "group" else col
                if col in ("rank", "n_active_rules", "n_obs"):
                    row[target] = to_int(val)
                elif col in ("score_hist_freq", "base_wr_holdout", "rsi14"):
                    row[target] = to_num(val)
                elif col.startswith("f_"):
                    row[target] = to_bool(val)
                else:
                    row[target] = val or None
            rows.append(row)
    upsert(base, key, "quant2_leaderboard", rows, "run_name,ticker", args.dry_run)

    # 4) MAE strike bridge
    with open(run_dir / "leaderboard" / "mae_strike_bridge.csv", newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            row = {"run_name": run_name, "sample": r["sample"], "n": to_int(r["n"]),
                   "mae_median": to_num(r["mae_median"])}
            for src, dst in TOUCH_RENAME.items():
                row[dst] = to_num(r.get(src))
            rows.append(row)
    upsert(base, key, "quant2_mae_strike_bridge", rows, "run_name,sample", args.dry_run)

    print("This is research and analysis only, not personalized financial advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
