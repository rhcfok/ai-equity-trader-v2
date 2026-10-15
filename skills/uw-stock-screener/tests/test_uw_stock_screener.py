import unittest

from scripts import uw_stock_screener as runner


class QueryAndDataTests(unittest.TestCase):
    def test_default_query_has_expected_guards(self):
        self.assertIn("@stock", runner.DEFAULT_QUERY)
        self.assertIn("marketcap >= 1B", runner.DEFAULT_QUERY)
        self.assertIn("distance_to_sma_50 <= 8%", runner.DEFAULT_QUERY)
        self.assertIn("week_52_high * 0.75", runner.DEFAULT_QUERY)
        self.assertIn("avg30_stock_volume >= 500K", runner.DEFAULT_QUERY)

    def test_exchange_validation_accepts_known_us_venues(self):
        self.assertEqual(runner.canonical_us_exchange("NASDAQGS"), "NASDAQ")
        self.assertEqual(runner.canonical_us_exchange("New York Stock Exchange"), "NYSE")
        self.assertEqual(runner.canonical_us_exchange("NYSE Arca"), "NYSE Arca")
        self.assertEqual(runner.canonical_us_exchange("XASE"), "NYSE American")

    def test_exchange_validation_rejects_unknown_and_country_only_values(self):
        self.assertIsNone(runner.canonical_us_exchange("LSE"))
        self.assertIsNone(runner.canonical_us_exchange("USA"))
        self.assertIsNone(runner.canonical_us_exchange(""))

    def test_momentum_excludes_latest_month(self):
        row = {"one_month_close": "110", "one_year_close": "100"}
        self.assertAlmostEqual(runner.momentum_ex_recent_month(row), 0.10)
        self.assertIsNone(runner.momentum_ex_recent_month({"one_month_close": 10, "one_year_close": 0}))

    def test_common_stock_only(self):
        self.assertTrue(runner.row_is_common_stock({}))
        self.assertTrue(runner.row_is_common_stock({"issue_type": "Common Stock"}))
        self.assertFalse(runner.row_is_common_stock({"issue_type": "ETF"}))


class WatchlistPlanTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {"symbol": "NEW", "exchange": "NASDAQ"},
            {"symbol": "WATCH", "exchange": "NYSE"},
            {"symbol": "SAT", "exchange": "NYSE"},
            {"symbol": "CORE", "exchange": "NASDAQ"},
            {"symbol": "OLD", "exchange": "NASDAQ"},
            {"symbol": "HEDGE", "exchange": "NYSE"},
        ]

    def test_new_ticker_is_added_as_watch1(self):
        actions = runner.plan_watchlist_actions(self.candidates[:1], [])
        self.assertEqual(actions[0]["action"], "add")
        self.assertEqual(actions[0]["category_after"], "watch1")

    def test_watch1_satellite_core_and_hedge_are_preserved(self):
        watchlist = [
            {"id": 1, "symbol": "WATCH", "category": "watch1"},
            {"id": 2, "symbol": "SAT", "category": "satellite"},
            {"id": 3, "symbol": "CORE", "category": "core"},
            {"id": 4, "symbol": "HEDGE", "category": "hedges"},
        ]
        actions = runner.plan_watchlist_actions(self.candidates[1:4] + self.candidates[5:], watchlist)
        self.assertTrue(all(action["action"] == "keep" for action in actions))
        self.assertEqual([action["category"] for action in actions], ["watch1", "satellite", "core", "hedges"])

    def test_lower_tier_existing_ticker_is_promoted(self):
        actions = runner.plan_watchlist_actions(
            [self.candidates[4]],
            [{"id": 5, "symbol": "OLD", "category": "watch2+"}],
        )
        self.assertEqual(actions[0]["action"], "promote")
        self.assertEqual(actions[0]["category_before"], "watch2+")
        self.assertEqual(actions[0]["category_after"], "watch1")
        self.assertEqual(actions[0]["watchlist_id"], 5)

    def test_symbol_matching_is_case_insensitive_and_protected_category_wins(self):
        actions = runner.plan_watchlist_actions(
            [{"symbol": "ABC", "exchange": "NASDAQ"}],
            [
                {"id": 8, "symbol": "abc", "category": "watch2"},
                {"id": 9, "symbol": "ABC", "category": "satellite"},
            ],
        )
        self.assertEqual(actions[0]["action"], "keep")
        self.assertEqual(actions[0]["category"], "satellite")


if __name__ == "__main__":
    unittest.main()
