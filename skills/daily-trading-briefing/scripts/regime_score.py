#!/usr/bin/env python3
"""regime_score.py — deterministic 0-100 market regime score for the daily briefing.

Replaces the prose-only Regime judgement in Step 3 of daily-trading-briefing with a
weighted composite of five pillars, each scored 0-100:

    Pillar    Weight  Inputs
    -------   ------  -------------------------------------------------------------
    trend      30%    SPY / QQQ vs 50- and 200-day SMA, SMA50 slope, 3-state Markov
                      (Bull / Sideways / Bear) next-session probabilities on SPY
    vol        25%    VIX level, VIX/VIX3M term structure, VIX 1y percentile,
                      variance risk premium (VIX - SPY 20d realised vol)
    breadth    20%    trade-stldc-v2 BULL vs BEAR confirms + alerts,
                      trade-jlaw-v2 chart constructive share (percentile vs own history),
                      Valid count across chart + yahoo
    flow       15%    UW market tide, 5-session average + share of positive sessions
                      (parsed from "flow-verdicts".payload.market_tide)
    stress     10%    Market Fragility Dashboard LPI tier + funding legs (x/3),
                      passed in by the agent (--fragility-tier / --funding-legs)

Missing pillars are dropped and the remaining weights renormalised; the output lists
which pillars were missing so the briefing can say so.

The score maps to a band with a fixed playbook (position size, allowed income
strategies, DTE cap). Hysteresis: the score is also computed for the two prior
sessions; the band only changes when the new band holds for 2 sessions or the score
is more than HYSTERESIS_PTS beyond the boundary.

Usage
-----
    python skills/daily-trading-briefing/scripts/regime_score.py \
        --date 2026-10-29 --fragility-tier ELEVATED --funding-legs 1 \
        --out . [--write-supabase] [--inputs-json inputs.json] [--dump-inputs]

Env: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (or lower-case supabase_url /
supabase_service_role in ./.env, same convention as pull_jlaw.py).
Dependency: requests only. Output: regime_<date>.json and regime_<date>.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

MODEL_VERSION = "regime-score v1.0"

WEIGHTS = {"trend": 0.30, "vol": 0.25, "breadth": 0.20, "flow": 0.15, "stress": 0.10}

# (lower bound inclusive, band) - evaluated top-down
BANDS = [
    (70.0, "RISK_ON"),
    (55.0, "CONSTRUCTIVE"),
    (45.0, "NEUTRAL"),
    (30.0, "CAUTIOUS"),
    (0.0, "DEFENSIVE"),
]
BAND_ORDER = [b for _, b in reversed(BANDS)]  # DEFENSIVE .. RISK_ON
HYSTERESIS_PTS = 3.0

BAND_ZH = {
    "RISK_ON": "进攻",
    "CONSTRUCTIVE": "建设性",
    "NEUTRAL": "中性",
    "CAUTIOUS": "谨慎",
    "DEFENSIVE": "防守",
}

# Fixed action map per band. size_mult applies to the Tier A/B base size.
PLAYBOOK: dict[str, dict[str, Any]] = {
    "RISK_ON": {
        "size_mult": 1.0,
        "tier_b_size_mult": 1.0,
        "new_longs": "A + B tiers",
        "income_strategies": ["WHEEL_CSP", "BULL_PUT_SPREAD", "SHORT_STRANGLE", "DIAGONAL_SPREAD", "WHEEL_CC"],
        "naked_csp": "all categories",
        "csp_dte_max": 45,
        "credit_at_risk_pct_nlv": 3.0,
    },
    "CONSTRUCTIVE": {
        "size_mult": 1.0,
        "tier_b_size_mult": 0.75,
        "new_longs": "A + B tiers",
        "income_strategies": ["WHEEL_CSP", "BULL_PUT_SPREAD", "DIAGONAL_SPREAD", "WHEEL_CC"],
        "naked_csp": "core + satellite",
        "csp_dte_max": 39,
        "credit_at_risk_pct_nlv": 2.5,
    },
    "NEUTRAL": {
        "size_mult": 0.6,
        "tier_b_size_mult": 0.5,
        "new_longs": "A tier only; B on trigger at half size",
        "income_strategies": ["WHEEL_CSP", "BULL_PUT_SPREAD", "WHEEL_CC", "BEAR_CALL_SPREAD"],
        "naked_csp": "core only",
        "csp_dte_max": 30,
        "credit_at_risk_pct_nlv": 2.0,
    },
    "CAUTIOUS": {
        "size_mult": 0.5,
        "tier_b_size_mult": 0.0,
        "new_longs": "A tier only, half size",
        "income_strategies": ["BULL_PUT_SPREAD", "WHEEL_CC", "BEAR_CALL_SPREAD"],
        "naked_csp": "none new (defined-risk spreads only)",
        "csp_dte_max": 21,
        "credit_at_risk_pct_nlv": 1.25,
    },
    "DEFENSIVE": {
        "size_mult": 0.25,
        "tier_b_size_mult": 0.0,
        "new_longs": "none; manage stops and hedges",
        "income_strategies": ["WHEEL_CC", "BEAR_CALL_SPREAD"],
        "naked_csp": "none",
        "csp_dte_max": 14,
        "credit_at_risk_pct_nlv": 0.5,
    },
}

FRAGILITY_TIER_SCORE = {
    "LOW": 90.0,
    "NORMAL": 70.0,
    "MODERATE": 60.0,
    "ELEVATED": 45.0,
    "HIGH": 20.0,
    "CRITICAL": 5.0,
}

TIDE_AVG_RE = re.compile(r"avg\s*(-)?\s*\$\s*(-)?\s*([\d,.]+)\s*([MB])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def lin(x: float, lo: float, hi: float) -> float:
    """Linear map x from [lo, hi] to [0, 1] (hi may be < lo for inverse maps)."""
    if hi == lo:
        return 0.5
    return clip01((x - lo) / (hi - lo))


def sma(xs: list[float], n: int) -> float | None:
    return mean(xs[-n:]) if len(xs) >= n else None


def percentile_rank(value: float, history: list[float]) -> float:
    """Share of history <= value, in [0, 1]."""
    if not history:
        return 0.5
    return sum(1 for h in history if h <= value) / len(history)


def band_for(score: float) -> str:
    for lo, name in BANDS:
        if score >= lo:
            return name
    return "DEFENSIVE"


def band_lower_bound(band: str) -> float:
    return next(lo for lo, name in BANDS if name == band)


def band_upper_bound(band: str) -> float:
    idx = BAND_ORDER.index(band)
    return band_lower_bound(BAND_ORDER[idx + 1]) if idx + 1 < len(BAND_ORDER) else 100.0


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_tide_avg_mm(note: str | None) -> float | None:
    """'... avg $-547M net call premium per 5-min bucket' -> -547.0 (in $M)."""
    if not note:
        return None
    m = TIDE_AVG_RE.search(note)
    if not m:
        return None
    try:
        val = float(m.group(3).replace(",", ""))
    except ValueError:
        return None
    if m.group(1) or m.group(2):
        val = -val
    return val * (1000.0 if m.group(4).upper() == "B" else 1.0)


def align_series(prices: dict[str, list[list[Any]]], anchor: str = "SPY") -> dict[str, list[float]]:
    """Align every symbol on the anchor's dates (forward-filling gaps)."""
    anchor_rows = [(str(d)[:10], float(c)) for d, c in prices.get(anchor, []) if c is not None]
    anchor_rows.sort()
    dates = [d for d, _ in anchor_rows]
    out: dict[str, list[float]] = {"__dates__": dates}  # type: ignore[dict-item]
    for sym, rows in prices.items():
        m = {str(d)[:10]: float(c) for d, c in rows if c is not None}
        series, last = [], None
        for d in dates:
            if d in m:
                last = m[d]
            series.append(last if last is not None else float("nan"))
        out[sym] = series
    return out


# ---------------------------------------------------------------------------
# Pillars (each returns score 0-100 + detail dict, or None if no data)
# ---------------------------------------------------------------------------


def markov_next_probs(closes: list[float], lookback: int = 250) -> dict[str, Any] | None:
    """3-state Markov chain on SPY: label each day Bull / Sideways / Bear by the
    vol-normalised 20d return (z > 0.5 Bull, z < -0.5 Bear), estimate the transition
    matrix from the trailing `lookback` labels, and return next-session probabilities
    from the current state. No look-ahead: only data up to the current bar is used."""
    if len(closes) < 82:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    labels: list[str] = []
    for i in range(60, len(rets)):
        sd = pstdev(rets[i - 60 : i]) or 1e-9
        r20 = sum(rets[i - 19 : i + 1])
        z = r20 / (sd * math.sqrt(20))
        labels.append("BULL" if z > 0.5 else "BEAR" if z < -0.5 else "SIDEWAYS")
    labels = labels[-lookback:]
    states = ["BULL", "SIDEWAYS", "BEAR"]
    counts = {a: {b: 1.0 for b in states} for a in states}  # Laplace smoothing
    for a, b in zip(labels, labels[1:]):
        counts[a][b] += 1.0
    cur = labels[-1]
    tot = sum(counts[cur].values())
    probs = {b: counts[cur][b] / tot for b in states}
    persistence = counts[cur][cur] / tot
    return {"state": cur, "p_next": {k: round(v, 3) for k, v in probs.items()}, "persistence": round(persistence, 3)}


def trend_pillar(aligned: dict[str, list[float]], k: int) -> tuple[float, dict] | None:
    checks: dict[str, bool] = {}
    for sym in ("SPY", "QQQ"):
        s = aligned.get(sym)
        if not s:
            continue
        xs = [x for x in (s[: len(s) - k] if k else s) if not math.isnan(x)]
        if len(xs) < 210:
            continue
        c, s50, s200 = xs[-1], sma(xs, 50), sma(xs, 200)
        s50_prev = mean(xs[-60:-10])
        checks[f"{sym}>SMA50"] = c > s50
        checks[f"{sym}>SMA200"] = c > s200
        checks[f"{sym} SMA50>SMA200"] = s50 > s200
        checks[f"{sym} SMA50 rising"] = s50 > s50_prev
    if not checks:
        return None
    spy = aligned.get("SPY") or []
    spy = [x for x in (spy[: len(spy) - k] if k else spy) if not math.isnan(x)]
    mk = markov_next_probs(spy)
    binary = sum(checks.values()) / len(checks)
    if mk:
        mk_comp = clip01((mk["p_next"]["BULL"] - mk["p_next"]["BEAR"] + 1.0) / 2.0)
        score = 100.0 * (0.7 * binary + 0.3 * mk_comp)
    else:
        score = 100.0 * binary
    return round(score, 1), {"checks": checks, "markov": mk}


def vol_pillar(aligned: dict[str, list[float]], k: int) -> tuple[float, dict] | None:
    def cut(sym: str) -> list[float]:
        s = aligned.get(sym) or []
        return [x for x in (s[: len(s) - k] if k else s) if not math.isnan(x)]

    vix, vix3m, spy = cut("^VIX"), cut("^VIX3M"), cut("SPY")
    if len(vix) < 30:
        return None
    parts: dict[str, float] = {}
    v = vix[-1]
    parts["vix_level"] = lin(v, 30.0, 12.0)
    parts["vix_pct_1y_inv"] = 1.0 - percentile_rank(v, vix[-252:])
    detail: dict[str, Any] = {"vix": round(v, 2)}
    if vix3m:
        ratio = v / vix3m[-1]
        parts["term_structure"] = lin(ratio, 1.05, 0.85)
        detail["vix_vix3m"] = round(ratio, 3)
    if len(spy) >= 21:
        rets = [math.log(spy[i] / spy[i - 1]) for i in range(len(spy) - 20, len(spy))]
        rv20 = pstdev(rets) * math.sqrt(252) * 100.0
        vrp = v - rv20
        parts["vrp"] = lin(vrp, -5.0, 8.0)
        detail["spy_rv20"] = round(rv20, 2)
        detail["vrp"] = round(vrp, 2)
    detail["parts"] = {kk: round(vv, 3) for kk, vv in parts.items()}
    return round(100.0 * mean(parts.values()), 1), detail


def breadth_pillar(stldc: list[dict], jlaw: list[dict], k: int) -> tuple[float, dict] | None:
    parts: dict[str, tuple[float, float]] = {}  # name -> (value 0-1, weight)
    detail: dict[str, Any] = {}
    st = sorted(stldc, key=lambda r: r["run_date"])
    st = st[: len(st) - k] if k else st
    if st:
        r = st[-1]
        bc, brc = r.get("bull_confirm", 0), r.get("bear_confirm", 0)
        ba, bra = r.get("bull_alert", 0), r.get("bear_alert", 0)
        if bc + brc:
            ratio = bc / (bc + brc)
            parts["stldc_confirm"] = (lin(ratio, 0.30, 0.70), 0.40)
            detail["stldc_bull_confirm_ratio"] = round(ratio, 3)
        if ba + bra:
            ratio_a = ba / (ba + bra)
            parts["stldc_alert"] = (lin(ratio_a, 0.30, 0.70), 0.15)
            detail["stldc_bull_alert_ratio"] = round(ratio_a, 3)
        detail["stldc_run_date"] = r["run_date"]
        detail["spy_regime_stldc"] = r.get("spy_regime")

    chart = sorted([r for r in jlaw if r.get("review_type") == "chart"], key=lambda r: r["run_date"])
    yahoo = sorted([r for r in jlaw if r.get("review_type") == "yahoo"], key=lambda r: r["run_date"])
    chart = chart[: len(chart) - k] if k else chart
    yahoo = yahoo[: len(yahoo) - k] if k else yahoo

    def share(r: dict) -> float | None:
        reviewed = r.get("valid", 0) + r.get("watch", 0) + r.get("skip", 0)
        return (r.get("valid", 0) + r.get("watch", 0)) / reviewed if reviewed else None

    if chart:
        shares = [s for s in (share(r) for r in chart[-21:]) if s is not None]
        if shares:
            cur = shares[-1]
            hist = shares[:-1]
            parts["jlaw_constructive_pctile"] = (percentile_rank(cur, hist) if len(hist) >= 5 else 0.5, 0.30)
            detail["jlaw_chart_constructive_share"] = round(cur, 3)
            detail["jlaw_history_runs"] = len(hist)
        valid_total = chart[-1].get("valid", 0) + (yahoo[-1].get("valid", 0) if yahoo else 0)
        parts["valid_presence"] = (clip01(valid_total / 5.0), 0.15)
        detail["valid_count"] = valid_total
        detail["jlaw_run_date"] = chart[-1]["run_date"]
    if not parts:
        return None
    wsum = sum(w for _, w in parts.values())
    score = 100.0 * sum(v * w for v, w in parts.values()) / wsum
    detail["parts"] = {kk: round(v, 3) for kk, (v, _) in parts.items()}
    return round(score, 1), detail


def flow_pillar(tide: list[dict], k: int) -> tuple[float, dict] | None:
    rows = sorted([t for t in tide if t.get("avg_mm") is not None], key=lambda r: r["date"])
    rows = rows[: len(rows) - k] if k else rows
    last5 = rows[-5:]
    if len(last5) < 2:
        return None
    vals = [r["avg_mm"] for r in last5]
    avg = mean(vals)
    pos_frac = sum(1 for v in vals if v > 0) / len(vals)
    score = 100.0 * (0.6 * lin(avg, -250.0, 250.0) + 0.4 * pos_frac)
    detail = {
        "tide_5d_avg_mm": round(avg, 1),
        "tide_pos_sessions": f"{sum(1 for v in vals if v > 0)}/{len(vals)}",
        "tide_latest_mm": vals[-1],
        "tide_latest_date": last5[-1]["date"],
        "tide_series": [(r["date"], r["avg_mm"]) for r in last5],
    }
    return round(score, 1), detail


def stress_pillar(fragility: dict | None) -> tuple[float, dict] | None:
    if not fragility or not fragility.get("lpi_tier"):
        return None
    tier = str(fragility["lpi_tier"]).upper()
    base = FRAGILITY_TIER_SCORE.get(tier)
    if base is None:
        return None
    legs = int(fragility.get("funding_legs") or 0)
    score = max(0.0, base - 10.0 * legs)
    return round(score, 1), {"lpi_tier": tier, "funding_legs": f"{legs}/3", "as_of": fragility.get("as_of")}


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def composite_at(inputs: dict, k: int) -> dict:
    aligned = align_series(inputs.get("prices") or {})
    pillars: dict[str, tuple[float, dict] | None] = {
        "trend": trend_pillar(aligned, k),
        "vol": vol_pillar(aligned, k),
        "breadth": breadth_pillar(inputs.get("stldc") or [], inputs.get("jlaw") or [], k),
        "flow": flow_pillar(inputs.get("tide") or [], k),
        "stress": stress_pillar(inputs.get("fragility")),
    }
    present = {n: p for n, p in pillars.items() if p is not None}
    missing = [n for n, p in pillars.items() if p is None]
    if not present:
        raise ValueError("no regime inputs available")
    wsum = sum(WEIGHTS[n] for n in present)
    score = sum(WEIGHTS[n] * p[0] for n, p in present.items()) / wsum
    dates = aligned.get("__dates__") or []
    as_of = dates[len(dates) - 1 - k] if len(dates) > k else None
    return {
        "as_of_session": as_of,
        "score": round(score, 1),
        "band": band_for(score),
        "pillars": {n: {"score": p[0], "weight": WEIGHTS[n], "detail": p[1]} for n, p in present.items()},
        "missing": missing,
    }


def apply_hysteresis(history: list[dict]) -> tuple[str, str]:
    """history = [t-2, t-1, t]. Returns (effective_band, note)."""
    cur = history[-1]
    if len(history) < 2:
        return cur["band"], "no history"
    prev = history[-2]
    if cur["band"] == prev["band"]:
        return cur["band"], "stable"
    # Changed today: switch only if far past the boundary, else hold prior band
    if BAND_ORDER.index(cur["band"]) > BAND_ORDER.index(prev["band"]):
        margin = cur["score"] - band_lower_bound(cur["band"])
    else:
        margin = band_upper_bound(cur["band"]) - cur["score"]
    if margin > HYSTERESIS_PTS:
        return cur["band"], f"switched {prev['band']}→{cur['band']} ({margin:.1f} pts past boundary)"
    return prev["band"], f"pending {prev['band']}→{cur['band']} (needs 2nd session or >{HYSTERESIS_PTS:.0f} pts)"


def detect_divergences(cur: dict) -> list[str]:
    p = {n: v["score"] for n, v in cur["pillars"].items()}
    notes: list[str] = []
    if p.get("trend", 50) >= 60 and p.get("breadth", 50) < 40:
        notes.append("Narrow leadership: index trend up but breadth weak | 指数趋势向上但广度偏弱（窄幅领涨）")
    if p.get("trend", 50) < 40 and p.get("breadth", 50) >= 60:
        notes.append("Breadth improving under a weak index trend | 指数趋势弱但广度改善（可能筑底）")
    if p.get("flow", 50) >= 65 and p.get("vol", 50) < 40:
        notes.append("Call-flow chasing into stressed vol | 期权流追多但波动率结构承压")
    if p.get("flow", 50) <= 35 and p.get("trend", 50) >= 60:
        notes.append("Tide defensive against an up-trend | tide 偏防御，与上升趋势背离")
    if p.get("stress", 50) < 40 and p.get("vol", 50) >= 60:
        notes.append("Fragility elevated while VIX is calm | 脆弱度偏高但 VIX 平静（低估尾部风险）")
    fd = cur["pillars"].get("flow", {}).get("detail", {})
    if fd and fd.get("tide_latest_mm") is not None and fd.get("tide_5d_avg_mm") is not None:
        if (fd["tide_latest_mm"] > 0) != (fd["tide_5d_avg_mm"] > 0):
            notes.append("Latest tide flipped vs 5d average, not yet confirmed | 当日 tide 与 5 日均值反向，未确认")
    mk = cur["pillars"].get("trend", {}).get("detail", {}).get("markov")
    stl = cur["pillars"].get("breadth", {}).get("detail", {}).get("spy_regime_stldc")
    if mk and stl and stl.upper() in {"BULL", "BEAR"} and mk["state"] != stl.upper() and mk["state"] != "SIDEWAYS":
        notes.append(f"Markov state {mk['state']} disagrees with STLDC SPY regime {stl} | Markov 与 STLDC SPY regime 不一致")
    return notes


def compute_regime(inputs: dict, report_date: str) -> dict:
    hist = []
    for k in (2, 1, 0):
        try:
            hist.append(composite_at(inputs, k))
        except ValueError:
            continue
    if not hist:
        raise ValueError("no regime inputs available")
    cur = hist[-1]
    eff, note = apply_hysteresis(hist)
    return {
        "model_version": MODEL_VERSION,
        "report_date": report_date,
        "as_of_session": cur["as_of_session"],
        "score": cur["score"],
        "raw_band": cur["band"],
        "band": eff,
        "band_zh": BAND_ZH[eff],
        "hysteresis": note,
        "score_history": [{"session": h["as_of_session"], "score": h["score"], "band": h["band"]} for h in hist],
        "pillars": cur["pillars"],
        "missing_pillars": cur["missing"],
        "divergences": detect_divergences(cur),
        "playbook": PLAYBOOK[eff],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Markdown (bilingual, Notion <table> syntax)
# ---------------------------------------------------------------------------

PILLAR_LABEL = {
    "trend": "趋势 Trend",
    "vol": "波动率 Vol",
    "breadth": "广度 Breadth",
    "flow": "期权流 Flow",
    "stress": "脆弱度 Stress",
}


def _pillar_evidence(name: str, d: dict) -> str:
    if name == "trend":
        checks = d.get("checks", {})
        passed = sum(checks.values())
        mk = d.get("markov") or {}
        pn = mk.get("p_next", {})
        mk_txt = f"; Markov {mk.get('state')} (P↑ {pn.get('BULL')}, P↓ {pn.get('BEAR')})" if mk else ""
        fails = [c for c, ok in checks.items() if not ok]
        return f"{passed}/{len(checks)} MA checks{mk_txt}" + (f"; fail: {', '.join(fails)}" if fails else "")
    if name == "vol":
        return f"VIX {d.get('vix')}, VIX/VIX3M {d.get('vix_vix3m')}, RV20 {d.get('spy_rv20')}, VRP {d.get('vrp')}"
    if name == "breadth":
        return (
            f"STLDC bull-confirm {d.get('stldc_bull_confirm_ratio')}, alerts {d.get('stldc_bull_alert_ratio')}; "
            f"chart constructive {d.get('jlaw_chart_constructive_share')}; Valid {d.get('valid_count')}"
        )
    if name == "flow":
        return f"5d avg {d.get('tide_5d_avg_mm')}M, positive {d.get('tide_pos_sessions')}, latest {d.get('tide_latest_mm')}M ({d.get('tide_latest_date')})"
    if name == "stress":
        return f"LPI {d.get('lpi_tier')}, funding {d.get('funding_legs')}, as-of {d.get('as_of')}"
    return ""


def render_markdown(res: dict) -> str:
    pb = res["playbook"]
    lines = [
        f"## 1. Regime 判定 | Market Regime — {res['score']:.0f}/100 · {res['band_zh']} {res['band']}",
        "",
        f"会话 Session: {res['as_of_session']} · 模型 {res['model_version']} · 迟滞 Hysteresis: {res['hysteresis']}",
        "",
        "<table header-row=\"true\">",
        "<tr><td>支柱 Pillar</td><td>权重 Weight</td><td>得分 Score</td><td>证据 Evidence</td></tr>",
    ]
    for name in ("trend", "vol", "breadth", "flow", "stress"):
        p = res["pillars"].get(name)
        if p is None:
            lines.append(f"<tr><td>{PILLAR_LABEL[name]}</td><td>{int(WEIGHTS[name]*100)}%</td><td>—</td><td>缺失 missing（权重已重分配 weight redistributed）</td></tr>")
            continue
        lines.append(
            f"<tr><td>{PILLAR_LABEL[name]}</td><td>{int(WEIGHTS[name]*100)}%</td><td>{p['score']:.0f}</td><td>{_pillar_evidence(name, p['detail'])}</td></tr>"
        )
    lines.append("</table>")
    lines.append("")
    trail = " → ".join(f"{h['session']}: {h['score']:.0f} {h['band']}" for h in res["score_history"])
    lines.append(f"近三日 3-session trail: {trail}")
    lines.append("")
    lines.append("**执行规则 Playbook**")
    lines.append("")
    lines.append(f"- 仓位 Size: A 级 ×{pb['size_mult']} · B 级 ×{pb['tier_b_size_mult']} · 新多头 New longs: {pb['new_longs']}")
    lines.append(f"- 收入策略 Income strategies: {', '.join(pb['income_strategies'])}")
    lines.append(f"- 裸卖 CSP Naked CSP: {pb['naked_csp']} · CSP DTE 上限 cap: {pb['csp_dte_max']}d · 信用风险预算 Credit-at-risk ≤ {pb['credit_at_risk_pct_nlv']}% NLV")
    if res["divergences"]:
        lines.append("")
        lines.append("**背离 Divergences**")
        lines.append("")
        for d in res["divergences"]:
            lines.append(f"- {d}")
    if res["missing_pillars"]:
        lines.append("")
        lines.append(f"缺失支柱 Missing pillars: {', '.join(res['missing_pillars'])} — 结论偏保守 treat as lower confidence")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Data acquisition (Yahoo chart API + Supabase REST)
# ---------------------------------------------------------------------------


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path.cwd() / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    url = env.get("supabase_url") or os.environ.get("SUPABASE_URL", "")
    key = (
        env.get("supabase_service_role")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE", "")
    )
    return {"url": url, "key": key}


def _rest_base(raw: str) -> str:
    raw = raw.rstrip("/")
    return raw if raw.endswith("/rest/v1") else raw + "/rest/v1"


def _get(session, url: str, params: dict | None = None, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def fetch_yahoo_closes(session, symbol: str, rng: str = "2y") -> list[list[Any]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = _get(session, url, {"range": rng, "interval": "1d", "includePrePost": "false"})
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append([datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), float(c)])
    return out


def fetch_supabase_inputs(session, base: str, key: str, report_date: str) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    session.headers.update(h)

    fv = _get(
        session,
        f"{base}/flow-verdicts",
        {"select": "verdict_date,payload->market_tide", "verdict_date": f"lte.{report_date}", "order": "verdict_date.desc", "limit": "10"},
    ).json()
    tide, seen = [], set()
    for r in fv:
        mt = r.get("market_tide") or {}
        d = str(mt.get("date") or r.get("verdict_date"))[:10]
        if d in seen:
            continue
        seen.add(d)
        tide.append({"date": d, "avg_mm": parse_tide_avg_mm(mt.get("note")), "bias": mt.get("bias")})

    st_rows = _get(
        session,
        f"{base}/trade-stldc-v2",
        {"select": "run_date,stage,spy_regime", "run_date": f"lte.{report_date}", "order": "run_date.desc", "limit": "5000"},
    ).json()
    st: dict[str, dict] = {}
    for r in st_rows:
        d = r["run_date"]
        e = st.setdefault(d, {"run_date": d, "bull_confirm": 0, "bear_confirm": 0, "bull_alert": 0, "bear_alert": 0, "spy_regime": r.get("spy_regime")})
        key_map = {"BULL-CONFIRM": "bull_confirm", "BEAR-CONFIRM": "bear_confirm", "BULL-ALERT": "bull_alert", "BEAR-ALERT": "bear_alert"}
        if r.get("stage") in key_map:
            e[key_map[r["stage"]]] += 1
    stldc = sorted(st.values(), key=lambda x: x["run_date"])[-5:]

    # trade-jlaw-v2 classification counts per run_date / review_type (paged)
    jl: dict[tuple[str, str], dict] = {}
    offset = 0
    while True:
        rows = _get(
            session,
            f"{base}/trade-jlaw-v2",
            {
                "select": "run_date,jlaw_review_type,classification",
                "run_date": f"lte.{report_date}",
                "order": "run_date.desc",
                "limit": "1000",
                "offset": str(offset),
            },
        ).json()
        if not rows:
            break
        for r in rows:
            k = (r["run_date"], r.get("jlaw_review_type") or "")
            e = jl.setdefault(k, {"run_date": k[0], "review_type": k[1], "valid": 0, "watch": 0, "skip": 0})
            c = str(r.get("classification") or "").lower()
            if c in ("valid", "watch", "skip"):
                e[c] += 1
        if len({k[0] for k in jl}) > 24 or len(rows) < 1000:
            break
        offset += 1000
    jlaw = sorted(jl.values(), key=lambda x: (x["run_date"], x["review_type"]))
    return {"tide": tide, "stldc": stldc, "jlaw": jlaw}


def gather_inputs(report_date: str, fragility: dict | None) -> dict:
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (regime-score)"})
    prices = {sym: fetch_yahoo_closes(s, sym) for sym in ("SPY", "QQQ", "^VIX", "^VIX3M")}
    inputs: dict[str, Any] = {"prices": prices, "fragility": fragility}
    env = load_env()
    if env["url"] and env["key"]:
        sb = requests.Session()
        inputs.update(fetch_supabase_inputs(sb, _rest_base(env["url"]), env["key"], report_date))
    else:
        print("WARN: Supabase credentials missing — breadth and flow pillars will be skipped", file=sys.stderr)
    return inputs


def upsert_supabase(res: dict) -> None:
    import requests

    env = load_env()
    if not (env["url"] and env["key"]):
        print("WARN: no Supabase credentials; skip upsert", file=sys.stderr)
        return
    row = {
        "report_date": res["report_date"],
        "as_of_session": res["as_of_session"],
        "score": res["score"],
        "raw_band": res["raw_band"],
        "band": res["band"],
        "hysteresis": res["hysteresis"],
        "pillars": res["pillars"],
        "missing_pillars": res["missing_pillars"],
        "divergences": res["divergences"],
        "playbook": res["playbook"],
        "score_history": res["score_history"],
        "model_version": res["model_version"],
    }
    r = requests.post(
        f"{_rest_base(env['url'])}/market_regime_daily",
        headers={
            "apikey": env["key"],
            "Authorization": f"Bearer {env['key']}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "report_date"},
        data=json.dumps(row, default=str),
        timeout=60,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", default=date.today().isoformat(), help="briefing date (YYYY-MM-DD)")
    ap.add_argument("--fragility-tier", default=None, help="LPI tier from the fragility dashboard (LOW/NORMAL/ELEVATED/HIGH/CRITICAL)")
    ap.add_argument("--funding-legs", type=int, default=0, help="funding resonance legs confirmed (0-3)")
    ap.add_argument("--fragility-as-of", default=None, help="dashboard as-of date")
    ap.add_argument("--inputs-json", default=None, help="skip fetching; read inputs from this JSON file")
    ap.add_argument("--dump-inputs", action="store_true", help="also write regime_inputs_<date>.json")
    ap.add_argument("--write-supabase", action="store_true", help="upsert into public.market_regime_daily")
    ap.add_argument("--out", default=".", help="output directory")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fragility = (
        {"lpi_tier": args.fragility_tier, "funding_legs": args.funding_legs, "as_of": args.fragility_as_of}
        if args.fragility_tier
        else None
    )
    if args.inputs_json:
        inputs = json.loads(Path(args.inputs_json).read_text(encoding="utf-8"))
        if fragility:
            inputs["fragility"] = fragility
    else:
        inputs = gather_inputs(args.date, fragility)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.dump_inputs:
        (out / f"regime_inputs_{args.date}.json").write_text(json.dumps(inputs, indent=1), encoding="utf-8")
    res = compute_regime(inputs, args.date)
    (out / f"regime_{args.date}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / f"regime_{args.date}.md").write_text(render_markdown(res), encoding="utf-8")
    if args.write_supabase:
        upsert_supabase(res)
    print(f"Regime {res['score']:.1f}/100 {res['band']} (raw {res['raw_band']}; {res['hysteresis']}); missing={res['missing_pillars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
