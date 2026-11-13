"""
run_daily.py - one-shot daily job for the market-regime-model skill.

1. Runs the full walk-forward regime pipeline (run_multi.main()) for
   SPY / QQQ / SOXX with frozen parameters.
2. Builds the daily report body (markdown, ready to drop inside a Notion
   <toggle> block) and writes it to out/regime_report_<today>.md.
3. Prints the report to stdout.

Usage (from anywhere):
    "D:/AI projects/AI-trading/.venv/Scripts/python.exe" -I run_daily.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pandas as pd  # noqa: E402

import run_multi  # noqa: E402  (main() is guarded, nothing runs at import)

from pipeline import trend_state  # noqa: E402

POSITIONS = {"Bull": 1.0, "Choppy": 0.25, "Bear": 0.0}


def build_report() -> str:
    summary = json.load(open(os.path.join(run_multi.OUTM, "summary_multi.json"),
                             encoding="utf-8"))
    as_of = summary["SPY"]["data"].split(" -> ")[1]
    asof_ts = pd.Timestamp(as_of)
    today = dt.date.today().isoformat()

    lines = []
    lines.append(f"**Data as of {as_of} (US close)** · generated {today} · "
                 "frozen walk-forward parameters · WK-means × 126d trend hybrid "
                 "(hysteresis θ=0.55, m=3)")
    lines.append("")
    lines.append("| Ticker | Regime | Trend | Dist | P(Bull) | P(Choppy) | P(Bear) | Days |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for t in run_multi.TICKERS:
        s = summary[t]
        p = s["probabilities"]
        close = pd.read_csv(os.path.join(run_multi.OUTM, t,
                                         f"{t.lower()}_daily.csv"),
                            index_col=0, parse_dates=True)["Close"]
        tr = trend_state(close, mom=126, band=0.02)
        trend = "Up" if tr.iloc[-1] > 0 else "Down"
        lines.append(
            f"| {t} | **{s['current_regime']}** | {trend} | "
            f"{s['current_dist_state']} | {p['Bull']:.2f} | "
            f"{p['Choppy']:.2f} | {p['Bear']:.2f} | {s['days_in_regime']} |")
    lines.append("")
    lines.append("*P(*) = WK-means distribution-shape probabilities (factor 1: "
                 "which 40-day return distribution the window resembles; the Bear "
                 "cluster = crash-like panics only). Trend = 126-day momentum "
                 "Schmitt trigger, ±2% deadband (factor 2 — catches grind-down "
                 "bears the distribution cannot see). Regime = hybrid of the two "
                 "(non-Bull distribution + down trend → Bear).*")
    lines.append("")

    lines.append("**Transitions (last 14 days):**")
    n_listed = 0
    for t in run_multi.TICKERS:
        tr = pd.read_csv(os.path.join(run_multi.OUTM, t,
                                      "transitions_hybrid.csv"),
                         parse_dates=["date"])
        recent = tr[tr["date"] >= asof_ts - pd.Timedelta(days=14)]
        for _, row in recent.iterrows():
            d = row["date"].strftime("%Y-%m-%d")
            age = (asof_ts - row["date"]).days
            flag = " — **new**" if age <= 3 else ""
            lines.append(f"- {t}: {row['from']} → **{row['to']}** on {d}{flag}")
            n_listed += 1
    if n_listed == 0:
        lines.append("- none")
    lines.append("")

    exp = sum(POSITIONS[summary[t]["current_regime"]]
              for t in run_multi.TICKERS) / len(run_multi.TICKERS)
    lines.append(f"**Composite benchmark exposure** (Bull 1.0 / Choppy 0.25 / "
                 f"Bear 0.0, equal-weight): **{exp:.0%}**")
    lines.append("")
    lines.append("*Research use only — not investment advice. Detection, not "
                 "prediction: single-day shocks are absorbed with a lag "
                 "(hysteresis); V-shaped flash corrections may be missed. "
                 "Method: Horváth–Issa–Muguruza (2021) Wasserstein k-means × "
                 "trend; see docs/ in the skill folder.*")

    return "\n".join(lines) + "\n"


def main() -> None:
    print("[1/2] Running pipeline (run_multi) ...", flush=True)
    run_multi.main()
    print("[2/2] Building report ...", flush=True)
    text = build_report()
    out_md = os.path.join(run_multi.OUTM,
                          f"regime_report_{dt.date.today().isoformat()}.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"report written: {out_md}")
    print("---REPORT-BEGIN---")
    print(text)
    print("---REPORT-END---")


if __name__ == "__main__":
    main()
