"""Expected Move backtest + 20-trading-day projection (LuxAlgo MC Expected Move model).

Model under test (Monte Carlo Expected Move Distribution [LuxAlgo], CC BY-NC-SA 4.0):
  - per-bar log-return vol:  sigma = stdev(log(close/close[1]), lookback)
  - optional drift:          mu    = log(SMA(close, lookback) / SMA(close, lookback)[1])
  - horizon-h terminal distribution is lognormal in log space:
        ln(S_T) ~ N(ln(S0) + h*mu, (sigma*sqrt(h))^2)
    The Pine script's p16/p50/p84 percentiles from 200 MC paths are noisy samples of
    exactly these closed-form quantiles, so we test the analytic bands (exact model)
    rather than re-injecting Monte Carlo sampling noise.
  - z_off = ln(S_T/S0) / (sigma*sqrt(h))          -> drift-off (neutral) band coverage
  - z_on  = (ln(S_T/S0) - h*mu) / (sigma*sqrt(h)) -> drift-on (trend-adjusted) coverage
  Both bands claim ~68% of outcomes inside +/-1 sigma. Fat tails push real coverage below.

Universe: all Supabase watchlist categories (core, satellite, watch1-3, euro, ...).
Creds: SUPABASE_URL / SUPABASE_KEY env vars, else the repo-root .env
Usage: python em_backtest.py [--horizon 30] [--lookback 50] [--years 5]
       [--proj-horizons 20,30] [--refresh]

Outputs (out/):
  em_anchor_rows.csv          one row per non-overlapping anchor observation
  em_coverage_by_ticker.csv   per-ticker coverage, drift on/off
  em_coverage_pooled.csv      pooled by category + all, Wilson CIs
  em_projection_{h}d.csv      per horizon: quantile bands + P(up in h days)
  em_diagnostics.png          z histogram vs normal, coverage by category, rolling coverage
  em_projection_core.png      fan charts (at --horizon), core bucket
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from em_model import DATA_DIR as OUT, load_creds  # noqa: E402

CATEGORY_ORDER = ("core", "satellite", "watch1", "watch2", "watch3", "euro")


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def fetch_watchlist() -> dict[str, list[str]]:
    url, key = load_creds()
    r = requests.get(
        f"{url}/rest/v1/watchlist?select=symbol,category&limit=2000",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30,
    )
    r.raise_for_status()
    buckets: dict[str, list[str]] = {}
    for row in r.json():
        buckets.setdefault(row["category"], []).append(row["symbol"])
    order = [c for c in CATEGORY_ORDER if c in buckets]
    order += [c for c in sorted(buckets) if c not in CATEGORY_ORDER]
    return {c: sorted(set(buckets[c])) for c in order}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def download_close(symbols: list[str], years: int) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}

    def try_chunk(chunk: list[str]) -> None:
        df = yf.download(
            chunk,
            period=f"{years}y",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,  # concurrent threads trigger Yahoo rate limits
        )
        if df is None or df.empty:
            return
        if isinstance(df.columns, pd.MultiIndex):
            level0 = set(df.columns.get_level_values(0))
            if level0 <= {"Open", "High", "Low", "Close", "Volume", "Adj Close"}:
                pairs = [(sym, sym in set(df.columns.get_level_values(1))) for sym in chunk]
                # yfinance >=1.x: columns are (field, ticker)
                for sym, present in pairs:
                    if present:
                        s = df.xs(sym, axis=1, level=1)["Close"].dropna()
                        if len(s):
                            out[sym] = s.astype(float)
            else:  # (ticker, field) orientation
                for sym in chunk:
                    if sym in level0:
                        s = df[sym]["Close"].dropna()
                        if len(s):
                            out[sym] = s.astype(float)
        else:
            s = df["Close"].dropna()
            if len(s):
                out[chunk[0]] = s.astype(float)

    chunk_size = 25
    todo = list(symbols)
    for attempt in (1, 2):  # second pass retries symbols Yahoo dropped
        for i in range(0, len(todo), chunk_size):
            chunk = todo[i : i + chunk_size]
            try_chunk(chunk)
            time.sleep(1.0)
        todo = [s for s in symbols if s not in out]
        if not todo:
            break
        if attempt == 1 and todo:
            print(f"  retrying {len(todo)} symbols that came back empty ...")
            time.sleep(3.0)
    return out


# ---------------------------------------------------------------------------
# Backtest: non-overlapping anchors
# ---------------------------------------------------------------------------
def anchor_rows(close: pd.Series, ticker: str, category: str,
                lookback: int, horizon: int) -> list[dict]:
    """One observation per non-overlapping anchor (step == horizon)."""
    if len(close) < lookback + horizon + 5:
        return []
    lr = np.log(close / close.shift(1))
    sma = close.rolling(lookback).mean()
    sig = lr.rolling(lookback).std(ddof=0)  # Pine ta.stdev is population stdev
    rows = []
    for i in range(lookback, len(close) - horizon, horizon):
        s0, sT = close.iloc[i], close.iloc[i + horizon]
        vol_h = sig.iloc[i] * math.sqrt(horizon)
        if not (vol_h and vol_h > 0) or s0 <= 0:
            continue
        ret = math.log(sT / s0)
        mu = math.log(sma.iloc[i] / sma.iloc[i - 1]) if sma.iloc[i - 1] > 0 else 0.0
        rows.append({
            "category": category,
            "ticker": ticker,
            "date0": close.index[i].date(),
            "s0": round(float(s0), 4),
            "sT": round(float(sT), 4),
            "vol20_pct": round(100 * (math.exp(vol_h) - 1), 2),
            "mu20_pct": round(100 * (math.exp(horizon * mu) - 1), 2),
            "z_off": round(ret / vol_h, 4),
            "z_on": round((ret - horizon * mu) / vol_h, 4),
            "drift_pred_up": mu > 0,
            "up_outcome": sT > s0,
        })
    return rows


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, center - half), min(1.0, center + half))


def coverage_block(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    cov_off = float((df.z_off.abs() <= 1).mean()) if n else float("nan")
    cov_on = float((df.z_on.abs() <= 1).mean()) if n else float("nan")
    lo_off, hi_off = wilson(cov_off, n)
    lo_on, hi_on = wilson(cov_on, n)
    return {
        "universe": label, "n_obs": n,
        "cov_off": round(100 * cov_off, 1), "ci_off_lo": round(100 * lo_off, 1),
        "ci_off_hi": round(100 * hi_off, 1),
        "cov_on": round(100 * cov_on, 1), "ci_on_lo": round(100 * lo_on, 1),
        "ci_on_hi": round(100 * hi_on, 1),
        "breach_up_off": round(100 * float((df.z_off > 1).mean()), 1) if n else None,
        "breach_dn_off": round(100 * float((df.z_off < -1).mean()), 1) if n else None,
        "breach_up_on": round(100 * float((df.z_on > 1).mean()), 1) if n else None,
        "breach_dn_on": round(100 * float((df.z_on < -1).mean()), 1) if n else None,
        "median_absz_off": round(float(df.z_off.abs().median()), 3) if n else None,
        "median_absz_on": round(float(df.z_on.abs().median()), 3) if n else None,
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def project(close: pd.Series, ticker: str, category: str,
            lookback: int, horizon: int) -> dict | None:
    close = close.dropna()
    if len(close) < lookback + 2 or close.iloc[-1] <= 0:
        return None
    lr = np.log(close / close.shift(1))
    sma = close.rolling(lookback).mean()
    vol_h = float(lr.rolling(lookback).std(ddof=0).iloc[-1]) * math.sqrt(horizon)
    s0 = float(close.iloc[-1])
    mu = math.log(float(sma.iloc[-1]) / float(sma.iloc[-2]))
    if not vol_h or vol_h <= 0:
        return None
    q = lambda z: round(s0 * math.exp(z * vol_h), 2)  # noqa: E731  neutral quantile
    p_up = 0.5 + 0.5 * math.erf((math.sqrt(horizon) * mu / vol_h) / math.sqrt(2))
    target = np.busday_offset(np.datetime64(close.index[-1].date()), horizon)
    return {
        "category": category,
        "ticker": ticker,
        "as_of": str(close.index[-1].date()),
        "target_date": str(pd.Timestamp(target).date()),
        "close": round(s0, 2),
        "em_band_pct": round(100 * (math.exp(vol_h) - 1), 1),  # +/-1 sigma, neutral
        "mu_h_pct": round(100 * (math.exp(horizon * mu) - 1), 1),  # drift center move
        "p5_off": q(-1.645), "p16_off": q(-1.0), "p50_off": q(0.0),
        "p84_off": q(1.0), "p95_off": q(1.645),
        "p16_on": round(s0 * math.exp(horizon * mu - vol_h), 2),
        "p50_on": round(s0 * math.exp(horizon * mu), 2),
        "p84_on": round(s0 * math.exp(horizon * mu + vol_h), 2),
        "p_up_pct": round(100 * p_up, 1),
    }


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def normal_pdf(x):
    return np.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def make_diagnostics(rows: pd.DataFrame, horizon: int, cats: list[str]) -> plt.Figure:
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))
    ax = axes[0]
    z = rows.z_off.clip(-5, 5).values
    ax.hist(z, bins=60, density=True, alpha=0.65, color="#2196f3", label="observed z")
    xs = np.linspace(-5, 5, 200)
    ax.plot(xs, normal_pdf(xs), "k--", lw=1.2, label="N(0,1)")
    ax.axvline(-1, color="#f23645", ls=":", lw=1); ax.axvline(1, color="#f23645", ls=":", lw=1)
    ax.set_title(f"z-off distribution vs normal (h={horizon})")
    ax.set_xlabel("z"); ax.legend(fontsize=8)

    ax = axes[1]
    cats_all = cats + ("ALL",)
    cov_off = [coverage_block(rows[rows.category == c], c)["cov_off"] for c in cats] + \
              [coverage_block(rows, "ALL")["cov_off"]]
    cov_on = [coverage_block(rows[rows.category == c], c)["cov_on"] for c in cats] + \
             [coverage_block(rows, "ALL")["cov_on"]]
    x = np.arange(len(cats_all))
    ax.bar(x - 0.18, cov_off, 0.36, label="drift OFF", color="#787b86")
    ax.bar(x + 0.18, cov_on, 0.36, label="drift ON", color="#2196f3")
    ax.axhline(68, color="#f23645", ls="--", lw=1, label="model claims 68%")
    ax.set_xticks(x, cats_all, fontsize=8); ax.set_ylim(0, 100)
    ax.set_ylabel("% outcomes inside +/-1 sigma band")
    ax.set_title("Empirical coverage by universe"); ax.legend(fontsize=8)

    ax = axes[2]
    rr = rows.sort_values("date0")
    roll = rr.z_off.abs().le(1).rolling(120, min_periods=20).mean() * 100
    ax.plot(pd.to_datetime(rr.date0), roll, color="#2196f3", lw=1.2)
    ax.axhline(68, color="#f23645", ls="--", lw=1)
    ax.set_title("Rolling pooled coverage (120-obs window)")
    ax.set_ylabel("% inside band"); ax.set_xlabel("anchor date")
    fig.tight_layout()
    return fig


def make_core_fans(proj: pd.DataFrame, closes: dict[str, pd.Series], horizon: int) -> plt.Figure:
    core = proj[proj.category == "core"].reset_index(drop=True)
    n = len(core)
    cols, nrows = 5, math.ceil(n / 5)
    fig, axes = plt.subplots(nrows, cols, figsize=(3.1 * cols, 2.4 * nrows))
    axes = np.atleast_2d(axes)
    hist_days = 120
    for k, (_, row) in enumerate(core.iterrows()):
        ax = axes[k // cols, k % cols]
        s = closes[row.ticker].dropna().iloc[-hist_days:]
        n_h = len(s)
        x_hist = np.arange(-n_h, 0)
        ax.plot(x_hist, s.values, color="#161616", lw=1.0)
        x_f = np.arange(0, horizon + 1)
        lo16, hi16 = row.p16_off, row.p84_off
        lo5, hi5 = row.p5_off, row.p95_off
        mid = row.p50_on
        ax.fill_between(x_f, lo5, hi5, color="#2196f3", alpha=0.12)
        ax.fill_between(x_f, lo16, hi16, color="#2196f3", alpha=0.30)
        ax.plot(x_f, [s.iloc[-1] + (mid - s.iloc[-1]) * t / horizon for t in x_f],
                color="#089981", lw=1.0, ls="--")
        ax.axhline(s.iloc[-1], color="#787b86", lw=0.6)
        ax.set_title(f"{row.ticker}  P(up)={row.p_up_pct:.0f}%", fontsize=8)
        ax.tick_params(labelsize=6); ax.set_xticks([])
    for k in range(n, nrows * cols):
        axes[k // cols, k % cols].axis("off")
    fig.suptitle(f"Core: {horizon}-trading-day expected move "
                 "(dark blue +/-1 sigma neutral, light 90%, green dashed drift median)", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--horizon", type=int, default=30, help="backtest + fan-chart horizon")
    ap.add_argument("--lookback", type=int, default=50)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--proj-horizons", default="20,30",
                    help="comma list of horizons for projection CSVs")
    ap.add_argument("--refresh", action="store_true", help="re-download bars, ignore cache")
    args = ap.parse_args()
    proj_horizons = sorted({int(x) for x in args.proj_horizons.split(",")} | {args.horizon})

    os.makedirs(OUT, exist_ok=True)
    buckets = fetch_watchlist()
    cats = list(buckets)
    all_syms = sorted({s for syms in buckets.values() for s in syms})
    print(f"Universe: {len(all_syms)} symbols "
          + ", ".join(f"{c}={len(buckets[c])}" for c in cats))

    cache = os.path.join(OUT, "em_closes.pkl")
    closes = pd.read_pickle(cache) if os.path.exists(cache) and not args.refresh else {}
    fresh = [s for s in all_syms if s not in closes]
    if fresh:
        print(f"Downloading {args.years}y daily bars for {len(fresh)} new symbols ...")
        closes.update(download_close(fresh, args.years))
    if fresh or not os.path.exists(cache):
        pd.to_pickle(closes, cache)
    print(f"Bars for {len(closes)} symbols "
          f"(cache {'refreshed' if args.refresh else 'used'}: {len(closes) - len(fresh)})"
          if not args.refresh else f"Bars for {len(closes)} symbols (fresh download)")

    missing = [s for s in all_syms
               if s not in closes or len(closes[s]) < args.lookback + args.horizon + 5]
    if missing:
        print(f"No/short data ({len(missing)}): {', '.join(missing)}")

    ticker_cat = {s: c for c in cats for s in buckets[c]}
    rows: list[dict] = []
    for sym, ser in closes.items():
        rows.extend(anchor_rows(ser, sym, ticker_cat[sym], args.lookback, args.horizon))
    anchors = pd.DataFrame(rows)
    anchors.to_csv(os.path.join(OUT, "em_anchor_rows.csv"), index=False)
    print(f"Backtest: {len(anchors)} non-overlapping anchor observations "
          f"(h={args.horizon}, lookback={args.lookback})")

    # coverage by ticker and pooled
    by_ticker = (anchors.groupby(["category", "ticker"])
                 .apply(lambda g: pd.Series(coverage_block(g, g.name[1])), include_groups=False)
                 .reset_index())
    by_ticker.to_csv(os.path.join(OUT, "em_coverage_by_ticker.csv"), index=False)

    pooled = pd.DataFrame(
        [coverage_block(anchors[anchors.category == c], c) for c in cats]
        + [coverage_block(anchors, "ALL")]
    )
    pooled.to_csv(os.path.join(OUT, "em_coverage_pooled.csv"), index=False)

    print("\n=== Pooled coverage (target 68% inside +/-1 sigma) ===")
    print(pooled.to_string(index=False))

    dpred = anchors[anchors.drift_pred_up]
    dneg = anchors[~anchors.drift_pred_up]
    print(f"\nDrift (SMA-slope) directional hit rate over {args.horizon}d: "
          f"up-calls {100 * dpred.up_outcome.mean():.1f}% of {len(dpred)}, "
          f"down-calls {100 * (~dneg.up_outcome).mean():.1f}% of {len(dneg)}")

    print("\nProjecting ...")
    proj_at_h = None
    for h in proj_horizons:
        projs = [p for p in (project(ser, sym, ticker_cat[sym], args.lookback, h)
                             for sym, ser in closes.items()) if p]
        proj = pd.DataFrame(projs).sort_values(["category", "ticker"])
        proj.to_csv(os.path.join(OUT, f"em_projection_{h}d.csv"), index=False)
        print(f"  {h}d projections for {len(proj)} tickers -> out/em_projection_{h}d.csv")
        if h == args.horizon:
            proj_at_h = proj

    make_diagnostics(anchors, args.horizon, tuple(cats)).savefig(
        os.path.join(OUT, "em_diagnostics.png"), dpi=140)
    make_core_fans(proj_at_h, closes, args.horizon).savefig(
        os.path.join(OUT, "em_projection_core.png"), dpi=140)
    print("Charts -> out/em_diagnostics.png, out/em_projection_core.png")


if __name__ == "__main__":
    main()
