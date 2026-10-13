#!/usr/bin/env python3
"""Deterministic tests for the monthly-box runner; no network or credentials."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import run_monthly_box_backtest as runner  # noqa: E402


class MonthlyBoxRunnerTests(unittest.TestCase):
    def synthetic_daily_data(self, periods: int = 330) -> pd.DataFrame:
        dates = pd.bdate_range("2023-01-03", periods=periods)
        close = 100 + np.arange(periods, dtype=float) * 0.35
        close[::17] -= 2.0
        return pd.DataFrame({
            "date": dates,
            "open": close - 0.10,
            "high": close + 0.75,
            "low": close - 0.75,
            "close": close,
            "volume": np.full(periods, 1_000_000.0),
            "adjclose": close,
        })

    def make_trade(self, ticker: str, category: str, signal: str, entry: str, exit_date: str, gross: float = 0.02) -> dict:
        return {
            "ticker": ticker, "category": category, "strategy": "prior_month_high_breakout",
            "signal_date": signal, "entry_date": entry, "exit_date": exit_date,
            "gross_return": gross, "net_return": runner.net_return_from_gross(gross, 20.0),
            "holding_calendar_days": 5,
        }

    def test_watchlist_keeps_only_us_priority_categories_without_truncation(self) -> None:
        rows = [
            {"id": 1, "symbol": "AAPL", "exchange": "NASDAQ", "category": "core"},
            {"id": 2, "symbol": "AAPL", "exchange": "NASDAQ", "category": "watch1"},
            {"id": 3, "symbol": "SHOP", "exchange": "TSX", "category": "core"},
            {"id": 4, "symbol": "MSFT", "exchange": "NASDAQ", "category": "satellite"},
            {"id": 5, "symbol": "", "exchange": "NYSE", "category": "watch1"},
            {"id": 6, "symbol": "NVDA", "exchange": "NASDAQ", "category": "watch2"},
        ]
        accepted, rejected = runner.prepare_watchlist(rows, ("core", "satellite", "watch1"), 1)
        self.assertEqual([item["ticker"] for item in accepted], ["AAPL", "MSFT"])
        self.assertEqual([item["category"] for item in accepted], ["core", "satellite"])
        self.assertIn("not_explicitly_us_listed", {item["reason"] for item in rejected})
        self.assertIn("duplicate_lower_priority_or_later_row", {item["reason"] for item in rejected})

    def test_supabase_watchlist_loader_paginates_full_pages(self) -> None:
        class Response:
            def __init__(self, payload, content_range):
                self.payload = payload
                self.headers = {"Content-Range": content_range}

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        first_page = [{"id": index, "symbol": f"S{index}", "exchange": "NYSE", "category": "core"} for index in range(runner.WATCHLIST_PAGE_SIZE)]
        second_page = [{"id": 1001, "symbol": "LAST", "exchange": "NASDAQ", "category": "watch1"}]
        with patch.object(runner.urllib.request, "urlopen", side_effect=[
            Response(first_page, "0-999/1001"), Response(second_page, "1000-1000/1001"),
        ]) as urlopen:
            rows = runner.load_watchlist_supabase("https://example.invalid", "key", "watchlist", ("core", "satellite", "watch1"))
        self.assertEqual(len(rows), runner.WATCHLIST_PAGE_SIZE + 1)
        self.assertEqual(urlopen.call_count, 2)

    def test_prior_month_level_never_uses_current_month_high(self) -> None:
        frame = self.synthetic_daily_data(70)
        frame.loc[frame["date"].dt.month == 2, "high"] += 1000.0
        features = runner.add_features(frame)
        jan_high = float(frame.loc[frame["date"].dt.month == 1, "high"].max())
        february = features[features["date"].dt.month == 2]
        self.assertTrue((february["prior_month_high"] == jan_high).all())
        self.assertLess(float(february["prior_month_high"].iloc[0]), float(february["high"].max()))

    def test_initial_balance_is_unavailable_before_fifth_session_completes(self) -> None:
        features = runner.add_features(self.synthetic_daily_data(70))
        month = features[features["month"] == features["month"].iloc[1]]
        self.assertTrue(month.iloc[:5]["ib5_high"].isna().all())
        self.assertTrue(month.iloc[5:]["ib5_high"].notna().all())

    def test_entries_are_next_open_and_stay_in_month(self) -> None:
        features = runner.add_features(self.synthetic_daily_data())
        trades = runner.backtest_ticker(features, "TEST", "core", 20.0)
        self.assertGreater(len(trades), 0)
        for trade in trades:
            signal_index = features.index[features["date"].dt.date.astype(str) == trade["signal_date"]][0]
            entry_index = features.index[features["date"].dt.date.astype(str) == trade["entry_date"]][0]
            exit_index = features.index[features["date"].dt.date.astype(str) == trade["exit_date"]][0]
            self.assertEqual(entry_index, signal_index + 1)
            self.assertEqual(features.loc[signal_index, "month"], features.loc[entry_index, "month"])
            self.assertGreater(exit_index, entry_index)
            self.assertAlmostEqual(trade["entry_price"], float(features.loc[entry_index, "open"]))
            self.assertAlmostEqual(trade["exit_price"], float(features.loc[exit_index, "open"]))
            self.assertLess(trade["net_return"], trade["gross_return"])

    def test_continuous_price_basis_records_adjustment_discontinuity(self) -> None:
        frame = self.synthetic_daily_data(12)
        frame.loc[:5, "adjclose"] = frame.loc[:5, "close"] * 0.5
        adjusted, events = runner.continuous_price_basis(frame)
        self.assertAlmostEqual(float(adjusted.loc[0, "open"]), float(frame.loc[0, "open"] * 0.5))
        self.assertGreaterEqual(len(events), 1)
        self.assertIn("raw_close", adjusted.columns)

    def test_cross_boundary_trades_are_purged_from_both_samples(self) -> None:
        boundary = pd.Timestamp("2024-02-01")
        trades = pd.DataFrame([
            self.make_trade("DEV", "core", "2024-01-10", "2024-01-11", "2024-01-16"),
            self.make_trade("CROSS", "satellite", "2024-01-30", "2024-01-31", "2024-02-02"),
            self.make_trade("HOLD", "watch1", "2024-02-05", "2024-02-06", "2024-02-12"),
        ])
        development, holdout, purged = runner.split_samples(trades, boundary)
        self.assertEqual(development["ticker"].tolist(), ["DEV"])
        self.assertEqual(holdout["ticker"].tolist(), ["HOLD"])
        self.assertEqual(purged["ticker"].tolist(), ["CROSS"])

    def test_validation_requires_all_predeclared_gates_and_flags_category_concentration(self) -> None:
        holdout_start = pd.Timestamp("2024-01-01")
        holdout_end = pd.Timestamp("2026-01-01")
        categories = ("core", "satellite", "watch1")
        trades = []
        for index in range(60):
            ticker = f"T{index % 10:02d}"
            category = categories[index % 3]
            entry = holdout_start + pd.Timedelta(days=10 + index * 10)
            signal = entry - pd.Timedelta(days=1)
            trades.append(self.make_trade(ticker, category, str(signal.date()), str(entry.date()), str((entry + pd.Timedelta(days=5)).date())))
        for index in range(60):
            ticker = f"T{index % 10:02d}"
            category = categories[index % 3]
            entry = pd.Timestamp("2023-01-02") + pd.Timedelta(days=index * 5)
            signal = entry - pd.Timedelta(days=1)
            trades.append(self.make_trade(ticker, category, str(signal.date()), str(entry.date()), str((entry + pd.Timedelta(days=3)).date())))
        scorecard, validation = runner.validation_scorecard(
            pd.DataFrame(trades), holdout_start, holdout_end, categories, 20.0, 200, True, True, True, True
        )
        row = scorecard[scorecard["strategy"] == "prior_month_high_breakout"].iloc[0]
        self.assertTrue(row["induction_review_eligible"])
        self.assertTrue(all(validation["strategies"]["prior_month_high_breakout"]["gates"].values()))

        concentrated = pd.DataFrame(trades)
        concentrated.loc[concentrated["category"] == "core", "gross_return"] = 0.20
        concentrated.loc[concentrated["category"] == "core", "net_return"] = concentrated.loc[concentrated["category"] == "core", "gross_return"].map(lambda value: runner.net_return_from_gross(value, 20.0))
        scorecard, _ = runner.validation_scorecard(
            concentrated, holdout_start, holdout_end, categories, 20.0, 100, True, True, True, True
        )
        row = scorecard[scorecard["strategy"] == "prior_month_high_breakout"].iloc[0]
        self.assertFalse(row["g9_category_concentration_lte_60pct"])

    def test_stale_or_incomplete_cache_is_rejected(self) -> None:
        frame = self.synthetic_daily_data(80)
        with self.assertRaises(runner.BacktestError):
            runner.validate_raw_history(frame, pd.Timestamp("2023-01-01"), pd.Timestamp("2024-12-31"))

    def test_material_interior_cache_gap_is_rejected(self) -> None:
        frame = self.synthetic_daily_data(330).drop(index=range(120, 180)).reset_index(drop=True)
        with self.assertRaises(runner.BacktestError):
            runner.validate_raw_history(frame, pd.Timestamp("2023-01-01"), frame["date"].iloc[-1])

    def test_review_requires_distinct_bound_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            review = {
                "run_name": "run", "code_sha256": "code", "config_sha256": "config",
                "run_operator_id": "runner-agent", "reviewer_agent_id": "review-agent",
                "reviewed_utc": "2026-10-13T00:00:00Z", "verdict": "INDUCTION REVIEW ELIGIBLE",
            }
            path.write_text(json.dumps(review))
            eligible, status = runner.load_independent_review(path, "run", "code", "config", "runner-agent")
            self.assertTrue(eligible)
            self.assertEqual(status["reviewer_agent_id"], "review-agent")
            review["reviewer_agent_id"] = "runner-agent"
            path.write_text(json.dumps(review))
            with self.assertRaises(runner.BacktestError):
                runner.load_independent_review(path, "run", "code", "config", "runner-agent")
            for invalid_identifier in ("runner-agent ", " review-agent ", " ", 42):
                review["reviewer_agent_id"] = invalid_identifier
                path.write_text(json.dumps(review))
                with self.assertRaises(runner.BacktestError):
                    runner.load_independent_review(path, "run", "code", "config", "runner-agent")
            for invalid_operator in ("", " runner-agent", "runner agent", 42):
                with self.assertRaises(runner.BacktestError):
                    runner.canonical_agent_id(invalid_operator, "run_operator_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
