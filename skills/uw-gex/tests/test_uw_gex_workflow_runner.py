#!/usr/bin/env python3
"""Offline regression tests for the prefixed Unusual Whales GEX workflow."""
from __future__ import annotations

import http.client
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "uw-gex_workflow_runner.py"
spec = importlib.util.spec_from_file_location("uw_gex_workflow_runner", RUNNER_PATH)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class UnusualWhalesGexRunnerTests(unittest.TestCase):
    @staticmethod
    def contract(strike, side, oi, gamma, dte, symbol="TEST260101"):
        suffix = "C" if side == "call" else "P"
        return {
            "strike": str(strike),
            "option_type": side,
            "open_interest": oi,
            "gamma": gamma,
            "dte": dte,
            "option_symbol": f"{symbol}{suffix}{int(float(strike) * 1000):08d}",
        }

    def test_default_watchlist_configuration(self):
        self.assertEqual(runner.EXPLICIT_SYMBOLS, [])
        self.assertEqual(runner.WATCHLIST_CATEGORIES, ("core", "satellite", "watch1"))

    def test_prepare_watchlist_symbols_orders_filters_and_deduplicates(self):
        rows = [
            {"id": 2, "symbol": "MSFT", "exchange": "NASDAQ", "category": "core"},
            {"id": 3, "symbol": "SHOP", "exchange": "TSX", "category": "core"},
            {"id": 4, "symbol": "MSFT", "exchange": "NASDAQ", "category": "satellite"},
            {"id": 5, "symbol": "AAPL", "exchange": "NASDAQ", "category": "watch1"},
            {"id": 6, "symbol": "IGNORED", "exchange": "NYSE", "category": "other"},
            {"id": 7, "symbol": "", "exchange": "NYSE", "category": "watch1"},
        ]
        self.assertEqual(runner.prepare_watchlist_symbols(rows), [
            {"symbol": "MSFT", "category": "core"},
            {"symbol": "AAPL", "category": "watch1"},
        ])

    def test_load_watchlist_symbols_requests_configured_categories(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'[{"id":1,"symbol":"AAPL","exchange":"NASDAQ","category":"core"}]'
        with patch.object(runner, "SUPABASE_URL", "https://example.test"), \
             patch.object(runner, "SUPABASE_KEY", "test-key"), \
             patch.object(runner.urllib.request, "urlopen", return_value=response) as mocked_open:
            self.assertEqual(runner.load_watchlist_symbols(), [{"symbol": "AAPL", "category": "core"}])
        request = mocked_open.call_args.args[0]
        self.assertIn("category=in.%28core%2Csatellite%2Cwatch1%29", request.full_url)
        self.assertIn("select=id%2Csymbol%2Cexchange%2Ccategory", request.full_url)

    def test_uw_get_retries_incomplete_response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data": []}'
        with patch.object(
            runner.urllib.request,
            "urlopen",
            side_effect=[http.client.IncompleteRead(b"partial", 10), response],
        ) as mocked_open, patch.object(runner.time, "sleep") as mocked_sleep:
            self.assertEqual(runner.uw_get("/test"), {"data": []})
        self.assertEqual(mocked_open.call_count, 2)
        mocked_sleep.assert_called_once_with(0.5)

    def test_aggregate_uses_nearest_dte_and_unsigned_sides(self):
        rows = [
            self.contract(100, "call", 100, 0.02, 1),
            self.contract(100, "put", 300, 0.01, 1),
            self.contract(100, "call", 9_999, 0.50, 10),
            self.contract(110, "call", 100, 0.10, 5),
            self.contract(90, "put", 100, 0.02, 4),
            self.contract(130, "call", 10_000, 0.50, 1),
            self.contract(105, "put", 49, 0.50, 1),
        ]
        summary = runner.aggregate(rows, 100.0, "2026-10-08")
        self.assertEqual(summary["strike_count"], 3)
        self.assertEqual(summary["call_wall"], 110.0)
        self.assertEqual(summary["put_wall"], 100.0)
        self.assertEqual(summary["call_wall_gex_mm"], 0.001)
        self.assertEqual(summary["put_wall_gex_mm"], -0.0003)
        self.assertEqual(summary["net_gex_mm"], 0.0007)
        self.assertEqual(summary["regime"], "sub_resolution")
        self.assertEqual(summary["signed_source"], "none")
        self.assertIsNone(summary["zero_gamma"])
        self.assertIn("sub_resolution", summary["validity"])

    def test_occ_symbol_fallback_handles_provider_contract_shape(self):
        parsed = runner.normalize_contract(
            {"option_symbol": "AAPL261016P00325000", "open_interest": 75, "gamma": "0.015"},
            "2026-10-16",
            8,
        )
        self.assertEqual(parsed, {
            "strike": 325.0,
            "option_type": "put",
            "open_interest": 75.0,
            "gamma": 0.015,
            "dte": 8,
            "expiry": "2026-10-16",
        })

    def test_select_expiries_uses_one_market_date_and_dte_window(self):
        trade_date, rows = runner.select_expiries(
            [
                {"date": "2026-10-08", "expiry": "2026-10-09", "dte": 1},
                {"date": "2026-10-08", "expiry": "2026-10-30", "dte": 22},
                {"date": "2026-10-08", "expiry": "2026-11-20", "dte": 43},
            ],
            0,
            30,
        )
        self.assertEqual(trade_date, "2026-10-08")
        self.assertEqual(rows, [
            {"expiry": "2026-10-09", "dte": 1},
            {"expiry": "2026-10-30", "dte": 22},
        ])

    def test_fetch_uses_gex_endpoint_only_as_expiry_calendar(self):
        calls = []

        def fake_uw_get(path, params=None):
            calls.append((path, params))
            if path.endswith("/greek-exposure/expiry"):
                return {"data": [
                    {"date": "2026-10-08", "expiry": "2026-10-09", "dte": 1, "call_gex": "999999"},
                    {"date": "2026-10-08", "expiry": "2026-11-20", "dte": 43, "call_gex": "999999"},
                ]}
            if path.endswith("/quote"):
                return {"data": {"last_trade": {"price": "100.25"}}}
            if path.endswith("/option-contracts"):
                return {"data": [
                    {"option_symbol": "TEST261009C00100000", "open_interest": 100, "gamma": "0.02"},
                    {"option_symbol": "TEST261009P00100000", "open_interest": 200, "gamma": "0.01"},
                ]}
            raise AssertionError(path)

        with patch.object(runner, "uw_get", side_effect=fake_uw_get):
            fetched = runner.fetch_unusual_whales("TEST")
        self.assertEqual(fetched["spot"], 100.25)
        self.assertEqual(fetched["trade_date"], "2026-10-08")
        self.assertEqual(fetched["expiries"], ["2026-10-09"])
        self.assertEqual(fetched["raw_count"], 2)
        self.assertEqual({row["option_type"] for row in fetched["rows"]}, {"call", "put"})
        self.assertEqual([path for path, _ in calls].count("/api/stock/TEST/option-contracts"), 1)

    def test_quote_spot_prefers_latest_trade_then_midpoint(self):
        self.assertEqual(runner.quote_spot({"data": {"last_trade": {"price": "123.45"}}}, "TEST"), 123.45)
        self.assertEqual(runner.quote_spot({"data": {"last_trade": {}, "quote_values": {"midpoint": "88.10"}}}, "TEST"), 88.10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
