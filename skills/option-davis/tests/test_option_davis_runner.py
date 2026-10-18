#!/usr/bin/env python3
"""Regression tests for the option-davis runner (offline; no network)."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "scripts"))

import option_davis_runner as runner


class ContractSymbolTests(unittest.TestCase):
    def test_build_osi_round_trip(self) -> None:
        self.assertEqual(runner.build_osi("CRM", "2026-11-20", "P", 230.0), "CRM261120P00230000")
        self.assertEqual(runner.build_osi("CRWD", "2026-11-20", "P", 197.5), "CRWD261120P00197500")
        self.assertEqual(runner.build_osi("SMH", "2026-11-20", "C", 575.0), "SMH261120C00575000")

    def test_parse_osi(self) -> None:
        self.assertEqual(runner.parse_osi("CRM261120P00230000"), (230.0, "P"))
        self.assertEqual(runner.parse_osi("SMH261120C00575000"), (575.0, "C"))
        self.assertIsNone(runner.parse_osi("not-a-contract"))


class CalendarTests(unittest.TestCase):
    def test_next_trading_day(self) -> None:
        self.assertEqual(runner.next_trading_day(date(2026, 10, 17)), date(2026, 10, 19))  # Sat -> Mon
        self.assertEqual(runner.next_trading_day(date(2026, 10, 16)), date(2026, 10, 16))  # Fri stays

    def test_pick_expiry(self) -> None:
        # Sat 2026-10-18: anchor Mon 10-19; Fridays at +25..39d are 11-13, 11-20, 11-27.
        expiry, dte = runner.pick_expiry(date(2026, 10, 18))
        self.assertEqual(expiry, "2026-11-20")
        self.assertEqual(dte, 32)


class LedgerRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        runner.EXP_MAIN = "2026-11-20"
        runner.DTE = {"2026-11-20": 32}
        runner.REPORT_DATE = "2026-10-18"
        runner.FLOW_DATE = "2026-10-15"
        runner.JLAW_DATE = "2026-10-16"
        runner.RUN_ID = "test-run"
        runner.NOW_ISO = "2026-10-18T00:00:00+00:00"

    @staticmethod
    def candidate(**overrides) -> dict:
        base = {
            "symbol": "CRM",
            "verdict": "bullish",
            "spot": 239.56,
            "iv_rank": 48.0,
            "iv30": 40.9,
            "tier": "MODERATE",
            "alert": True,
            "jlaw": 11,
            "class": "Watch",
            "option_score": 6.0,
            "iv_trend": "RISING",
            "pcr": 0.69,
            "below_high": 9.2,
            "put_wall": 240.0,
            "call_wall": 245.0,
            "pw_pct": 2.7,
            "cw_pct": 1.3,
            "flow_bias": "insufficient_evidence",
            "bullish_z": 0.25,
            "bearish_z": -0.3,
            "net_bias_z": 0.2558,
            "earn": "2026-12-02",
            "earn_ok": True,
            "reasons": ["call_ask_dominant"],
            "key_flows": [],
            "gex_ts": "2026-10-16T06:00:00+00:00",
            "gex_net": 0.09,
            "gravity": "up",
            "dp": None,
            "dp_net_mm": None,
            "dp_accumulating": None,
            "score": 12.5,
        }
        base.update(overrides)
        return base

    @staticmethod
    def put(strike: float, bid: float, ask: float, delta: float, oi: float = 1000.0) -> dict:
        return {"strike": strike, "ctype": "P", "bid": bid, "ask": ask, "iv": 0.4, "delta": -delta, "oi": oi}

    def test_short_put_row_economics_and_flags(self) -> None:
        short = self.put(230.0, 6.75, 7.69, 0.33)
        row = runner.opportunity_row(self.candidate(), "SHORT_PUT", short, None, 239.56)
        self.assertIsNotNone(row)
        self.assertEqual(row["opportunity_key"], "2026-10-18|SHORT_PUT|CRM|2026-11-20|230")
        self.assertEqual(row["short_option_symbol"], "CRM261120P00230000")
        self.assertIsNone(row["long_option_symbol"])
        self.assertAlmostEqual(row["entry_credit"], 6.75)
        self.assertAlmostEqual(row["collateral"], 223.25)
        self.assertAlmostEqual(row["max_loss"], 223.25)
        self.assertAlmostEqual(row["annualized_pct"], round(6.75 / 230 / 32 * 365 * 100, 2))
        self.assertTrue(row["davis_annualized_ok"])
        self.assertTrue(row["delta_band_ok"])
        self.assertEqual(row["caution"], "DP_NO_DATA")
        self.assertIsNone(row["dp_net_mm"])
        self.assertIn("dp_accumulating", row)

    def test_spread_row_economics(self) -> None:
        short = self.put(125.0, 7.00, 7.30, 0.31)
        long = self.put(120.0, 5.0, 5.30, -0.2)
        row = runner.opportunity_row(
            self.candidate(symbol="SMTC", jlaw=8, score=11.0, tier="WEAK", earn="2026-11-10", earn_ok=False),
            "BULL_PUT_SPREAD", short, long, 134.0,
        )
        self.assertIsNotNone(row)
        credit = round(7.00 - 5.30, 4)
        self.assertAlmostEqual(row["entry_credit"], credit)
        self.assertAlmostEqual(row["max_loss"], round(5 - credit, 4))
        self.assertEqual(row["short_option_symbol"], "SMTC261120P00125000")
        self.assertEqual(row["long_option_symbol"], "SMTC261120P00120000")
        self.assertIn("EARNINGS_INSIDE_WINDOW", row["caution"])

    def test_zero_credit_rejected(self) -> None:
        short = self.put(230.0, 0.0, 0.0, 0.3)
        self.assertIsNone(runner.opportunity_row(self.candidate(), "SHORT_PUT", short, None, 239.56))

    @staticmethod
    def dp_map(net: float, accumulating: bool) -> dict:
        return {
            "dp_as_of": "2026-10-17T00:00:00+00:00",
            "dp_total_premium_mm": 12.5,
            "dp_buy_mm": 7.5,
            "dp_sell_mm": 5.0,
            "dp_net_mm": round(net / 1e6, 2),
            "dp_blocks": 5,
            "dp_top_level": 300.0,
            "dp_top_level_mm": 8.0,
            "dp_accumulating": accumulating,
        }

    def test_dp_map_merged_into_row(self) -> None:
        short = self.put(230.0, 6.75, 7.69, 0.33)
        candidate = self.candidate(dp=self.dp_map(2_500_000, True), dp_net_mm=2.5, dp_accumulating=True)
        row = runner.opportunity_row(candidate, "SHORT_PUT", short, None, 239.56)
        self.assertEqual(row["dp_net_mm"], 2.5)
        self.assertEqual(row["dp_blocks"], 5)
        self.assertTrue(row["dp_accumulating"])
        self.assertNotIn("DP_NO_DATA", row["caution"] or "")
        self.assertNotIn("DP_DISTRIBUTION", row["caution"] or "")

    def test_dp_distribution_caution_only_for_non_bear(self) -> None:
        short = self.put(230.0, 6.75, 7.69, 0.33)
        call = {"strike": 250.0, "ctype": "C", "bid": 6.0, "ask": 6.4, "iv": 0.4, "delta": 0.31, "oi": 500.0}
        candidate = self.candidate(dp=self.dp_map(-2_200_000, False), dp_net_mm=-2.2, dp_accumulating=False)
        row_put = runner.opportunity_row(candidate, "SHORT_PUT", short, None, 239.56)
        self.assertIn("DP_DISTRIBUTION", row_put["caution"])
        row_bear = runner.opportunity_row(candidate, "BEAR_CALL_SPREAD", call, None, 239.56)
        self.assertNotIn("DP_DISTRIBUTION", row_bear["caution"] or "")

    def test_delta_band_and_annualized_flags(self) -> None:
        short = self.put(220.0, 4.0, 4.5, 0.19)  # below band
        row = runner.opportunity_row(self.candidate(), "SHORT_PUT", short, None, 239.56)
        self.assertFalse(row["delta_band_ok"])
        self.assertTrue(row["davis_annualized_ok"])  # 4/220/32*365 ~ 20.7%
        short_low = self.put(200.0, 0.5, 0.6, 0.15)
        row_low = runner.opportunity_row(self.candidate(), "SHORT_PUT", short_low, None, 239.56)
        self.assertFalse(row_low["davis_annualized_ok"])
        self.assertIn("ANNUALIZED_BELOW_20", row_low["caution"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
