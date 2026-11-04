#!/usr/bin/env python3
"""Offline regression tests for the weekly income board policy and rendering."""
from __future__ import annotations

import re
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_weekly_income_board as board_renderer  # noqa: E402


# Minimal manifest context follows the historical 2026-10-30 board schema. The
# spread rows below are copied from that dated fixture for test data only; they
# are not current quotes, recommendations, or trading instructions.
HISTORICAL_SPREAD_BOARD = [
    {
        "rank": 1,
        "ticker": "SPY",
        "qty": 1,
        "strategy": "BCS",
        "contract": "short SPY261120C00790000 / long SPY261120C00800000",
        "expiry": "2026-11-20",
        "dte": 23,
        "short_bid": 3.39,
        "long_ask": 1.49,
        "reference_credit_per_contract": 190,
        "max_loss_per_contract": 810,
        "return_on_risk": 0.2346,
        "short_delta": 0.2289,
        "short_oi": 21421,
        "short_volume": None,
        "flow": "bearish / zero conflicts",
        "stldc": "JLaw Watch 12",
        "dark_pool": "-$916.55M distribution",
        "earnings": "N/A ETF",
        "confidence": "High",
        "source": "Historical UW opportunity ledger / 2026-10-28",
        "gate": "Reconfirm fresh NBBO, risk, event, and reconciliation data.",
    },
    {
        "rank": 2,
        "ticker": "V",
        "qty": 1,
        "strategy": "BPS",
        "contract": "short V261120P00400000 / long V261120P00390000",
        "expiry": "2026-11-20",
        "dte": 21,
        "short_bid": 3.75,
        "long_ask": 1.92,
        "reference_credit_per_contract": 183,
        "max_loss_per_contract": 817,
        "return_on_risk": 0.224,
        "short_delta": -0.259,
        "short_oi": 1384,
        "short_volume": 354,
        "flow": "bullish",
        "stldc": "BULL-CONFIRM",
        "dark_pool": "+$34.7M accumulation",
        "earnings": "2027-02-04",
        "confidence": "High",
        "source": "Historical UW options screener / 2026-10-30",
        "gate": "Reconfirm fresh NBBO, risk, event, and reconciliation data.",
    },
    {
        "rank": 3,
        "ticker": "QQQ",
        "qty": 1,
        "strategy": "BCS",
        "contract": "short QQQ261120C00720000 / long QQQ261120C00730000",
        "expiry": "2026-11-20",
        "dte": 21,
        "short_bid": 5.55,
        "long_ask": 3.33,
        "reference_credit_per_contract": 222,
        "max_loss_per_contract": 778,
        "return_on_risk": 0.285,
        "short_delta": 0.252,
        "short_oi": 30641,
        "short_volume": 11742,
        "flow": "bearish",
        "stldc": "BEAR-ALERT",
        "dark_pool": "+$159.4M accumulation",
        "earnings": "N/A ETF",
        "confidence": "Medium",
        "source": "Historical UW options screener / 2026-10-30",
        "gate": "Reconfirm fresh NBBO, risk, event, and reconciliation data.",
    },
    {
        "rank": 4,
        "ticker": "DELL",
        "qty": 1,
        "strategy": "BPS",
        "contract": "short DELL261120P00400000 / long DELL261120P00390000",
        "expiry": "2026-11-20",
        "dte": 21,
        "short_bid": 18.75,
        "long_ask": 16.75,
        "reference_credit_per_contract": 200,
        "max_loss_per_contract": 800,
        "return_on_risk": 0.25,
        "short_delta": -0.349,
        "short_oi": 3538,
        "short_volume": 2051,
        "flow": "bullish",
        "stldc": "BEAR-CONFIRM",
        "dark_pool": "+$21.2M accumulation",
        "earnings": "2026-11-24",
        "confidence": "Low",
        "source": "Historical UW options screener / 2026-10-30",
        "gate": "Reconfirm fresh NBBO, risk, event, and reconciliation data.",
    },
]


def operating_plan():
    return [
        {
            "week": f"Week {index}",
            "date_range": f"2026-11-{index:02d} to 2026-11-{index + 1:02d}",
            "catalysts": "Re-screen the event calendar.",
            "book_action": "Review existing exposure before opening anything new.",
            "re_strike_queue": "Queue only after a fresh qualifying screen.",
            "conditions_and_vetoes": "Require fresh quotes, risk, and event checks.",
            "strategies": "Use only the permitted, currently qualified structures.",
            "credit_plan": "Reference only; no forecast or guaranteed credit.",
        }
        for index in range(1, 5)
    ]


def make_manifest(board=None, *, selection_mode=None, effective_regime="NEUTRAL"):
    rules = {
        "total_contracts": 20,
        "selection_mode": "full_50_50",  # Explicit compatibility fixture, not new-run default.
        "max_contracts_per_ticker": 2,
        "single_put_contracts": 10,
        "spread_contracts": 10,
        "csp_dte_cap": 35,
        "spread_dte_cap": 45,
    }
    if selection_mode is not None:
        rules["selection_mode"] = selection_mode
    return {
        "as_of": "Historical test snapshot from 2026-10-30; not live",
        "quote_basis": "Historical fixture values for offline tests only.",
        "effective_regime": effective_regime,
        "board_rule": rules,
        "board": deepcopy(HISTORICAL_SPREAD_BOARD if board is None else board),
        "budget": {
            "weekly_reference_range": [5000, 6000],
            "monthly_reference_range": [20000, 30000],
        },
        "four_week_plan": operating_plan(),
        "portfolio_context": {
            "snapshot_as_of": "Historical test context; not a current broker statement.",
            "summary": "Offline regression fixture only.",
            "triage_rule": "Review existing risk before considering a new sleeve.",
            "credit_separation": "Historical reference credits are excluded from live P&L.",
        },
    }


def synthetic_row(rank, ticker, strategy, qty=2):
    row = {
        "rank": rank,
        "ticker": ticker,
        "qty": qty,
        "strategy": strategy,
        "contract": f"short {ticker} test leg / long {ticker} test hedge",
        "expiry": "2026-11-20",
        "dte": 21,
        "short_bid": 1.25,
        "short_ask": 1.35,
        "long_bid": 0.45,
        "long_ask": 0.50,
        "reference_credit_per_contract": 75,
        "max_loss_per_contract": 925,
        "return_on_risk": 75 / 925,
        "short_delta": -0.25,
        "delta": -0.25,
        "short_oi": 1000,
        "short_volume": 100,
        "open_interest": 1000,
        "volume": 100,
        "flow": "test-only neutral evidence",
        "stldc": "test-only screen",
        "dark_pool": "test-only context",
        "earnings": "No event in test fixture",
        "confidence": "Medium",
        "source": "Synthetic offline regression fixture",
        "gate": "Test-only; not a recommendation.",
    }
    if strategy in {"SP", "CSP", "WHEEL_CSP"}:
        row["collateral_per_contract"] = 10000
    return row


def full_board():
    rows = []
    for index in range(5):
        rows.append(synthetic_row(index + 1, f"PUT{index}", "SP"))
    for index in range(5):
        rows.append(synthetic_row(index + 6, f"SPR{index}", "BPS"))
    return rows


class WeeklyIncomeBoardTests(unittest.TestCase):
    def test_cautious_and_defensive_accept_small_bps_bcs_only_board(self):
        for regime in ("CAUTIOUS", "DEFENSIVE"):
            with self.subTest(regime=regime):
                manifest = make_manifest(
                    selection_mode="regime_spread_only", effective_regime=regime
                )
                validated, totals = board_renderer.validate(manifest)
                self.assertEqual(len(validated), 4)
                self.assertEqual(totals["contracts"], 4)
                self.assertEqual(totals["single_put_contracts"], 0)
                self.assertEqual(totals["spread_contracts"], 4)
                self.assertEqual({row["strategy"] for row in validated}, {"BPS", "BCS"})

    def test_spread_only_mode_requires_explicit_poor_regime(self):
        for regime in (None, "NEUTRAL"):
            with self.subTest(regime=regime):
                manifest = make_manifest(
                    selection_mode="regime_spread_only", effective_regime=regime
                )
                with self.assertRaises(ValueError):
                    board_renderer.validate(manifest)

    def test_spread_only_render_shows_actual_mix_without_false_target_claims(self):
        manifest = make_manifest(
            selection_mode="regime_spread_only", effective_regime="CAUTIOUS"
        )
        rendered = board_renderer.render(manifest)
        allocation_line = next(
            (line for line in rendered.splitlines() if "Allocation" in line), ""
        )
        self.assertTrue(allocation_line, "render must include the actual allocation")
        self.assertRegex(allocation_line, r"\b0\b")
        self.assertRegex(allocation_line, r"\b4\b")
        self.assertNotIn("= 50:50", allocation_line)
        self.assertNotIn("50:50", allocation_line)
        self.assertNotRegex(
            rendered.lower(), r"within (?:the )?\$?5,?000.*weekly planning band"
        )

    def test_default_full_board_remains_valid_and_balanced(self):
        manifest = make_manifest(full_board(), effective_regime="NEUTRAL")
        validated, totals = board_renderer.validate(manifest)
        self.assertEqual(len(validated), 10)
        self.assertEqual(totals["contracts"], 20)
        self.assertEqual(totals["single_put_contracts"], 10)
        self.assertEqual(totals["spread_contracts"], 10)

    def test_single_put_family_has_no_blanket_poor_regime_veto(self):
        for regime in ("CAUTIOUS", "DEFENSIVE"):
            for strategy in ("SP", "CSP", "WHEEL_CSP"):
                with self.subTest(regime=regime, strategy=strategy):
                    rows = full_board()
                    rows[0]["strategy"] = strategy
                    manifest = make_manifest(rows, effective_regime=regime)
                    _, totals = board_renderer.validate(manifest)
                    self.assertEqual(totals["single_put_contracts"], 10)

    def test_dte_boundaries_remain_enforced(self):
        spread_manifest = make_manifest(
            selection_mode="regime_spread_only", effective_regime="CAUTIOUS"
        )
        spread_manifest["board"][0]["dte"] = 44
        board_renderer.validate(spread_manifest)
        spread_manifest["board"][0]["dte"] = 45
        with self.assertRaises(ValueError):
            board_renderer.validate(spread_manifest)

        single_manifest = make_manifest(full_board(), effective_regime="NEUTRAL")
        single_manifest["board"][0]["dte"] = 35
        board_renderer.validate(single_manifest)
        single_manifest["board"][0]["dte"] = 36
        with self.assertRaises(ValueError):
            board_renderer.validate(single_manifest)

    def test_invalid_quotes_max_loss_and_ticker_concentration_are_rejected(self):
        mutations = (
            ("short quote", lambda row: row.update(short_bid=0)),
            ("long quote", lambda row: row.update(long_ask=0)),
            ("max loss", lambda row: row.update(max_loss_per_contract=0)),
            ("ticker concentration", lambda row: row.update(qty=3)),
        )
        for label, mutate in mutations:
            with self.subTest(invalid_field=label):
                manifest = make_manifest(
                    selection_mode="regime_spread_only", effective_regime="DEFENSIVE"
                )
                mutate(manifest["board"][0])
                with self.assertRaises(ValueError):
                    board_renderer.validate(manifest)


if __name__ == "__main__":
    unittest.main()
