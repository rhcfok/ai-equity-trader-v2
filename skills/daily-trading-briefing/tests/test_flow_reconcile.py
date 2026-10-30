#!/usr/bin/env python3
"""Tests for scripts/flow_reconcile.py (freshness, reconciliation, board label check)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import flow_reconcile as fr  # noqa: E402

AS_OF = date(2026, 10, 29)


def sig(ticker, d, net, unusual, conf=0.6, z=None):
    return {"ticker": ticker, "signal_date": d, "net_directional_premium": net, "unusual_premium": unusual,
            "directional_confidence": conf, "net_bias_z": z}


def test_weekdays_between_skips_weekend():
    assert fr.weekdays_between(date(2026, 10, 23), date(2026, 10, 26)) == 1  # Fri -> Mon
    assert fr.weekdays_between(date(2026, 10, 13), date(2026, 10, 29)) == 12
    assert fr.weekdays_between(AS_OF, AS_OF) == 0


def test_freshness_flags_only_real_staleness():
    rep = fr.freshness_report(
        {"option_flow_symbol_signals": "2026-10-28", "flow_verdict_rows": "2026-10-29", "darkpool_symbol_daily": "2026-10-23", "gex_snapshot": None},
        AS_OF,
    )
    by = {r["table"]: r["status"] for r in rep}
    assert by == {"option_flow_symbol_signals": "ok", "flow_verdict_rows": "ok", "darkpool_symbol_daily": "stale", "gex_snapshot": "missing"}


def test_darkpool_duplicate_snapshot():
    s = [
        {"fetch_date": "2026-10-27", "rows": 146, "net": -5.0e8, "total": 2.9e10},
        {"fetch_date": "2026-10-26", "rows": 147, "net": -2.026e9, "total": 3.0e10},
        {"fetch_date": "2026-10-25", "rows": 147, "net": -2.026e9, "total": 3.0e10},
    ]
    assert fr.darkpool_duplicate(s) == ["2026-10-26 == 2026-10-25"]


def test_reconcile_mu_meta_conflicts_and_confirmed():
    inputs = {
        "verdict_rows": [
            {"symbol": "MU", "verdict_date": "2026-10-29", "verdict": "bearish", "alert_count": 20},
            {"symbol": "META", "verdict_date": "2026-10-29", "verdict": "bullish", "alert_count": 20},
            {"symbol": "AVGO", "verdict_date": "2026-10-29", "verdict": "bullish", "alert_count": 20},
        ],
        "signal_rows": [
            sig("MU", "2026-10-28", 448e6, 600e6, 0.8, 2.16),
            sig("META", "2026-10-28", -104e6, 190e6, 0.7, -2.73),
            sig("AVGO", "2026-10-28", 60e6, 100e6, 0.7, 1.1),
            sig("AVGO", "2026-10-27", 40e6, 80e6, 0.7, 0.9),
        ],
        "darkpool_rows": [
            {"symbol": "META", "net_premium": -2.85e9, "total_premium": 4.0e9},
            {"symbol": "AVGO", "net_premium": 3.0e8, "total_premium": 1.0e9},
        ],
    }
    rows = {r["symbol"]: r for r in fr.reconcile(inputs, AS_OF)}
    assert rows["MU"]["status"] == "conflict" and "verdict_vs_premium" in rows["MU"]["flow_conflicts"]
    assert rows["META"]["status"] == "conflict" and "verdict_vs_premium" in rows["META"]["flow_conflicts"]
    assert rows["AVGO"]["status"] == "confirmed"
    md = fr.render_markdown({"date": "2026-10-29", "freshness": [], "darkpool_duplicates": [], "reconciliation": list(rows.values())})
    assert "**MU**" in md and "AVGO" in md


def test_stale_verdict_not_confirmed():
    inputs = {
        "verdict_rows": [{"symbol": "AVGO", "verdict_date": "2026-10-20", "verdict": "bullish", "alert_count": 20}],
        "signal_rows": [sig("AVGO", "2026-10-28", 60e6, 100e6)],
        "darkpool_rows": [{"symbol": "AVGO", "net_premium": 3.0e8, "total_premium": 1.0e9}],
    }
    row = fr.reconcile(inputs, AS_OF)[0]
    assert row["verdict_stale"] is True and row["status"] != "confirmed"


BOARD = """
| # | 标的 | 策略 | 信号 |
|---|---|---|---|
| 7 | **PANW** | CSP ~390P | dp +$139M 吸筹 • bullish verdict（财报 11-18） |
| 8 | **CAT** | CSP ~150P | dp +$158M 吸筹；verdict 待核 — Low-Medium |
| 9 | **AVGO** | CSP ~340P | bullish verdict • STLDC Bull |
1. option_flow_symbol_signals 停更 10-13（net_z 断档 12 天）— 连续第五日提醒
数据源：option_flow_symbol_signals 10-13（stale 12 天）； gex_snapshot 10-29T04:42Z × 254
"""


def test_board_check_catches_panw_cat_and_false_staleness():
    verdicts = {"PANW": "mixed", "CAT": "mixed", "AVGO": "bullish"}
    fresh = fr.freshness_report({"option_flow_symbol_signals": "2026-10-28", "gex_snapshot": "2026-10-29"}, AS_OF)
    issues = fr.check_board(BOARD, verdicts, fresh)
    kinds = [(i["type"], i.get("symbol") or i.get("table")) for i in issues]
    assert ("verdict_mismatch", "PANW") in kinds
    assert ("pending_but_stored", "CAT") in kinds
    assert ("false_staleness", "option_flow_symbol_signals") in kinds
    assert not any(k[1] == "AVGO" for k in kinds)
    assert not any(k[1] == "gex_snapshot" for k in kinds)


def test_board_check_notion_table_rows():
    text = "<table><tr><td>**PANW**</td><td>bullish verdict</td></tr><tr><td>**AVGO**</td><td>verdict: bullish</td></tr></table>"
    issues = fr.check_board(text, {"PANW": "mixed", "AVGO": "bullish"})
    assert [(i["symbol"], i["claimed"]) for i in issues] == [("PANW", "bullish")]


def test_cli_offline_strict(tmp_path):
    import json

    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({
        "latest_dates": {"option_flow_symbol_signals": "2026-10-28"},
        "verdict_rows": [{"symbol": "PANW", "verdict_date": "2026-10-29", "verdict": "mixed"}],
        "signal_rows": [], "darkpool_rows": [], "darkpool_summaries": [],
    }))
    board = tmp_path / "b.md"
    board.write_text("**PANW** CSP • bullish verdict\n")
    assert fr.main(["--date", "2026-10-29", "--inputs-json", str(inp), "--check-board", str(board), "--out", str(tmp_path), "--strict"]) == 1
    assert (tmp_path / "flow_reconcile_2026-10-29.md").exists()
