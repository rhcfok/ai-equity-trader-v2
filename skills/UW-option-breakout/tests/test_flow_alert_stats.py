#!/usr/bin/env python3
"""Tests for scripts/flow_alert_stats.py (uncapped, premium-weighted alert stats)."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "scripts"))

import flow_alert_stats as fas  # noqa: E402
from upsert_flow_verdict_rows import merge_stats  # noqa: E402

AS_OF = date(2026, 10, 29)


def alert(kind, prem, ask=0.0, bid=0.0, dte=20, sweep=False, ts=None, idx=None):
    return {
        "id": idx,
        "type": kind,
        "total_premium": str(prem),
        "total_ask_side_prem": str(ask),
        "total_bid_side_prem": str(bid),
        "expiry": (AS_OF + timedelta(days=dte)).isoformat(),
        "has_sweep": sweep,
        "strike": "100",
        "created_at": ts or "2026-10-28T15:00:00Z",
    }


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.ok = status == 200

    def json(self):
        return {"data": self._data}


class FakeSession:
    """Serves `total` alerts newest-first, honouring limit + older_than."""

    def __init__(self, total):
        base = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)
        self.rows = [
            alert("call", 1000, ask=1000, ts=(base - timedelta(minutes=i)).isoformat(), idx=i) for i in range(total)
        ]
        self.calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls += 1
        rows = self.rows
        if params.get("older_than"):
            cut = datetime.fromisoformat(params["older_than"])
            rows = [r for r in rows if datetime.fromisoformat(r["created_at"]) < cut]
        return FakeResponse(rows[: int(params["limit"])])


class AggregateTests(unittest.TestCase):
    def test_side_attribution(self):
        stats = fas.aggregate_alerts(
            [
                alert("call", 500_000, ask=400_000, bid=100_000),  # bull 400k, bear 100k
                alert("put", 200_000, ask=50_000, bid=150_000),  # bull 150k, bear 50k
                alert("call", 50_000, ask=50_000, sweep=True),
            ],
            AS_OF,
        )
        self.assertEqual(stats["alert_count_total"], 3)
        self.assertEqual(stats["bull_premium"], 600_000)
        self.assertEqual(stats["bear_premium"], 150_000)
        self.assertAlmostEqual(stats["premium_tilt"], 0.6)
        self.assertEqual(stats["verdict_quant"], "bullish")
        self.assertAlmostEqual(stats["sweep_share"], round(50_000 / 750_000, 4))
        self.assertEqual(stats["largest_alert"]["side"], "ask")

    def test_bearish_and_tenor_weight(self):
        stats = fas.aggregate_alerts(
            [alert("put", 900_000, ask=900_000), alert("call", 400_000, ask=400_000, dte=400), alert("call", 1, ask=1)],
            AS_OF,
        )
        # LEAPS call ask-side counts at 0.25 -> 100k bull vs 900k bear
        self.assertEqual(stats["bull_premium"], 100_001)
        self.assertEqual(stats["verdict_quant"], "bearish")

    def test_protective(self):
        stats = fas.aggregate_alerts(
            [alert("put", 4_000_000, ask=4_000_000, dte=500), alert("call", 200_000, ask=200_000), alert("put", 1, ask=1)],
            AS_OF,
        )
        self.assertLess(stats["premium_tilt"], 0)
        self.assertEqual(stats["verdict_quant"], "protective")

    def test_no_data_and_mixed(self):
        self.assertEqual(fas.aggregate_alerts([alert("call", 50_000, ask=50_000)], AS_OF)["verdict_quant"], "no-data")
        mixed = fas.aggregate_alerts(
            [alert("call", 500_000, ask=500_000), alert("put", 450_000, ask=450_000), alert("put", 1, ask=1)], AS_OF
        )
        self.assertEqual(mixed["verdict_quant"], "mixed")

    def test_verdict_agree(self):
        self.assertTrue(fas.verdict_agrees("bullish", "bullish"))
        self.assertTrue(fas.verdict_agrees("protective", "bearish"))
        self.assertTrue(fas.verdict_agrees("mixed", "no-data"))
        self.assertFalse(fas.verdict_agrees("bearish", "bullish"))
        self.assertIsNone(fas.verdict_agrees(None, "bullish"))


class PaginationTests(unittest.TestCase):
    def test_pages_past_20_and_200(self):
        session = FakeSession(450)
        alerts, capped, pages = fas.fetch_flow_alerts(
            session, "k", "MU", datetime(2026, 10, 24, tzinfo=timezone.utc), max_pages=5, pause_s=0
        )
        self.assertEqual(len(alerts), 450)
        self.assertEqual(pages, 3)
        self.assertFalse(capped)

    def test_capped_flag(self):
        session = FakeSession(450)
        alerts, capped, pages = fas.fetch_flow_alerts(
            session, "k", "MU", datetime(2026, 10, 24, tzinfo=timezone.utc), max_pages=2, pause_s=0
        )
        self.assertEqual(len(alerts), 400)
        self.assertTrue(capped)


class MergeTests(unittest.TestCase):
    def test_merge_into_rows(self):
        rows = [{"symbol": "MU", "verdict": "bearish"}, {"symbol": "AAPL", "verdict": "mixed"}]
        stats = {"MU": {"verdict_quant": "bullish", "alert_count_total": 312, "premium_tilt": 0.7}}
        self.assertEqual(merge_stats(rows, stats), 1)
        self.assertFalse(rows[0]["verdict_agree"])
        self.assertEqual(rows[0]["alert_count_total"], 312)
        self.assertNotIn("verdict_quant", rows[1])


if __name__ == "__main__":
    unittest.main()
