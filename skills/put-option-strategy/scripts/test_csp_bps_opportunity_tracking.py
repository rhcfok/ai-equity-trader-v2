#!/usr/bin/env python3
"""Offline tests for CSP/BPS opportunity persistence and EOD accounting."""
from __future__ import annotations

import unittest

from csp_bps_opportunity_log_runner import build_log_rows, eod_update


class OpportunityTrackingTests(unittest.TestCase):
    def candidate(self, strategy: str = "CSP") -> dict:
        result = {
            "strategy": strategy,
            "symbol": "TEST",
            "exchange": "NASDAQ",
            "expiry": "2026-10-23",
            "dte": 10,
            "short_put": {
                "option_symbol": "TEST261023P00095000",
                "strike": 95,
                "delta": -0.32,
                "bid": 1.40,
                "ask": 1.60,
                "last_tape_time": "2026-10-13T20:00:00Z",
            },
            "safety_floor": 95,
            "safety_floor_source": "JLAW_AND_GEX",
            "earnings_date": "2026-11-20",
            "alignment_score": 5,
            "spot": 100,
            "credit": 140,
            "collateral": 9360,
            "reward_metric": 140 / 9360,
            "break_even": 93.6,
            "reason": "Test qualification.",
            "caution": "Test caution.",
            "source_context": {
                "jlaw": {"run_date": "2026-10-13", "classification": "Valid", "stop_loss": 95, "option_iv_rank": 45},
                "quant": {"as_of": "2026-10-13", "rank": 12, "n_active_rules": 5},
                "gex": {"snapshot_ts": "2026-10-13T20:00:00Z", "regime": "positive", "put_wall": 96},
                "earnings": {"date": "2026-11-20"},
            },
        }
        if strategy == "BPS":
            result.update({
                "long_put": {"option_symbol": "TEST261023P00085000", "strike": 85, "bid": 0.80, "ask": 1.00},
                "width": 10,
                "credit": 250,
                "max_loss": 750,
                "reward_metric": 1 / 3,
                "break_even": 92.5,
            })
        return result

    def test_builds_ranked_eligible_log_row(self) -> None:
        audit = {"as_of": "2026-10-13", "config": {}, "ranked_top20": [self.candidate()]}
        rows = build_log_rows(audit, "# board", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_status"], "ELIGIBLE")
        self.assertEqual(rows[0]["entry_credit"], 140.0)
        self.assertEqual(rows[0]["eod_outcome"], "PENDING")
        self.assertEqual(rows[0]["quant_rank"], 12)
        self.assertEqual(rows[0]["audit_json"]["top20_rank"], 1)

    def test_creates_no_setup_record(self) -> None:
        audit = {"as_of": "2026-10-13", "ranked_top20": []}
        rows = build_log_rows(audit, "# board", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_status"], "NO_QUALIFIED_SETUP")
        self.assertEqual(rows[0]["eod_outcome"], "NOT_APPLICABLE")

    def test_marks_csp_hypothetical_win_from_conservative_exit(self) -> None:
        update = eod_update({"strategy": "CSP", "entry_credit": 140}, {"short_bid": 0.90, "short_ask": 1.00}, "2026-10-13")
        self.assertEqual(update["eod_close_debit"], 100.0)
        self.assertEqual(update["eod_mark_to_market_pnl"], 40.0)
        self.assertEqual(update["eod_outcome"], "WIN")

    def test_marks_bps_hypothetical_loss_from_conservative_exit(self) -> None:
        update = eod_update({"strategy": "BPS", "entry_credit": 250}, {"short_ask": 4.0, "long_bid": 0.50}, "2026-10-13")
        self.assertEqual(update["eod_close_debit"], 350.0)
        self.assertEqual(update["eod_mark_to_market_pnl"], -100.0)
        self.assertEqual(update["eod_outcome"], "LOSS")

    def test_missing_quote_is_not_marked_win_or_loss(self) -> None:
        update = eod_update({"strategy": "CSP", "entry_credit": 140}, None, "2026-10-13")
        self.assertEqual(update["eod_outcome"], "QUOTE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
