#!/usr/bin/env python3
"""Regression tests for the trade-jlaw-v2 Unusual Whales three-stage pipeline."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "scripts"))

from fetch_unusual_whales_option_data import build_manifest, normalize_rows
from report_trade_jlaw_v2_option_breakouts import report_rows
from trade_jlaw_v2_option_contract import OPTION_COLUMNS, db_patch_payload
from transform_unusual_whales_option_data import transform_manifest


class TradeJlawV2OptionPipelineTests(unittest.TestCase):
    @staticmethod
    def source_rows() -> list[dict]:
        return [
            {
                "id": 11,
                "run_date": "2026-10-08",
                "symbol": "test",
                "jlaw_review_type": "yahoo",
                "classification": "Watch",
                "jlaw_score": 12,
                "current_price": 100.0,
                "regime": "Bull",
            },
            {
                "id": 12,
                "run_date": "2026-10-08",
                "symbol": "TEST",
                "jlaw_review_type": "manual",
                "classification": "Valid",
                "jlaw_score": 14,
                "current_price": 100.0,
                "regime": "Bull",
            },
        ]

    @staticmethod
    def snapshot_rows() -> list[dict]:
        dates = pd.bdate_range(end="2026-10-08", periods=260)
        implied = [0.15] * 256 + [0.10, 0.11, 0.12, 0.20]
        prices = [100.0] * 240 + [95.0 if index % 2 == 0 else 100.0 for index in range(20)]
        return [
            {
                "date": day.strftime("%Y-%m-%d"),
                "close": price,
                "volatility_30": iv,
                "iv_rank": 50.0 if index == len(dates) - 1 else 10.0,
                "call_volume": 300 if index == len(dates) - 1 else 100,
                "put_volume": 200,
                "avg_30_day_call_volume": 100,
            }
            for index, (day, iv, price) in enumerate(zip(dates, implied, prices))
        ]

    def test_fetch_manifest_deduplicates_by_enhanced_jlaw_identity(self) -> None:
        rows = normalize_rows(self.source_rows(), "2026-10-08")
        self.assertEqual(len(rows), 2)
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = build_manifest(rows, "2026-10-08", Path(tmpdir) / "snapshots", 500)
        self.assertEqual(len(manifest["snapshot_requests"]), 1)
        self.assertEqual(manifest["snapshot_requests"][0]["arguments"]["ticker"], "TEST")
        self.assertEqual(manifest["snapshot_requests"][0]["arguments"]["date"], "2026-10-08")

    def test_transform_maps_all_option_columns_without_changing_jlaw_fields(self) -> None:
        rows = normalize_rows(self.source_rows(), "2026-10-08")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshots = Path(tmpdir) / "snapshots"
            snapshots.mkdir()
            manifest = build_manifest(rows, "2026-10-08", snapshots, 500)
            (snapshots / "TEST.json").write_text(json.dumps({"structuredContent": {"result": self.snapshot_rows()}}), encoding="utf-8")
            updates, failures = transform_manifest(manifest)
        self.assertEqual(failures, [])
        self.assertEqual(len(updates), 2)
        update = updates[0]
        self.assertEqual(update["symbol"], "TEST")
        self.assertEqual(update["jlaw_review_type"], "manual")
        self.assertEqual(set(OPTION_COLUMNS), set(update).intersection(OPTION_COLUMNS))
        self.assertEqual(update["option_score"], 8.0)
        self.assertTrue(update["option_alert"])
        self.assertFalse(update["option_a1"])
        self.assertFalse(update["option_a6"])
        self.assertEqual(db_patch_payload(update), {column: update[column] for column in OPTION_COLUMNS})
        self.assertNotIn("jlaw_score", db_patch_payload(update))
        self.assertNotIn("classification", db_patch_payload(update))

    def test_report_is_ranked_and_contains_identity_context(self) -> None:
        rows = normalize_rows(self.source_rows(), "2026-10-08")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshots = Path(tmpdir) / "snapshots"
            snapshots.mkdir()
            manifest = build_manifest(rows, "2026-10-08", snapshots, 500)
            (snapshots / "TEST.json").write_text(json.dumps({"structuredContent": {"result": self.snapshot_rows()}}), encoding="utf-8")
            updates, failures = transform_manifest(manifest)
        report = report_rows(updates)
        self.assertEqual(failures, [])
        self.assertEqual(len(report), 2)
        self.assertEqual(report[0]["rank"], 1)
        self.assertEqual(report[0]["symbol"], "TEST")
        self.assertIn(report[0]["jlaw_review_type"], {"manual", "yahoo"})
        self.assertEqual(report[0]["option_score"], 8.0)
        self.assertIn("A2", report[0]["active_rules"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
