#!/usr/bin/env python3
"""Offline tests for the Part 3.0a full-universe STLDC ticker rosters."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_daily_trading_plan_v2.py"
SPEC = importlib.util.spec_from_file_location("briefing_builder_roster", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
AS_OF = "2026-10-30"


def row(symbol, stage, run_date=AS_OF):
    return {"symbol": symbol, "stage": stage, "run_date": run_date}


def scope(*symbols):
    return {s: {"symbol": s, "exchange": "NASDAQ"} for s in symbols}


class TestSTLDCRoster(unittest.TestCase):
    def test_four_separate_groups(self):
        rows = [row("A", "BULL-ALERT"), row("B", "BULL-CONFIRM"),
                row("C", "BEAR-ALERT"), row("D", "BEAR-CONFIRM")]
        groups, excluded = builder.stldc_stage_roster(rows, scope("A", "B", "C", "D"), AS_OF)
        self.assertEqual(list(groups), list(builder.STLDC_ROSTER_STAGES))
        self.assertEqual(list(groups.values()), [["A"], ["B"], ["C"], ["D"]])
        self.assertEqual(excluded, [])

    def test_sorted_deduplicated_and_normalized(self):
        rows = [row("Z", "BULL-ALERT"), row(" a ", "bull_alert"), row("A", "BULL-ALERT")]
        groups, excluded = builder.stldc_stage_roster(rows, scope("A", "Z"), AS_OF)
        self.assertEqual(groups["BULL-ALERT"], ["A", "Z"])
        self.assertEqual(excluded, [])

    def test_non_us_and_unverified_exchange_excluded(self):
        rows = [row(s, "BULL-CONFIRM") for s in ["US", "UK", "MISSING", "UNKNOWN"]]
        watch = scope("US")
        watch.update({"UK": {"exchange": "LSE"}, "UNKNOWN": {"exchange": None}})
        groups, excluded = builder.stldc_stage_roster(rows, watch, AS_OF)
        self.assertEqual(groups["BULL-CONFIRM"], ["US"])
        self.assertEqual(excluded, ["MISSING", "UK", "UNKNOWN"])

    def test_date_must_match_source_as_of(self):
        rows = [row("OK", "BEAR-ALERT"), row("OLD", "BEAR-ALERT", "2026-10-29"),
                row("NODATE", "BEAR-ALERT", None)]
        groups, excluded = builder.stldc_stage_roster(rows, scope("OK", "OLD", "NODATE"), AS_OF)
        self.assertEqual(groups["BEAR-ALERT"], ["OK"])
        self.assertEqual(excluded, ["NODATE", "OLD"])

    def test_conflicting_and_unknown_stage_fail_closed(self):
        rows = [row("X", "BULL-ALERT"), row("X", "BEAR-CONFIRM"), row("Y", "UNKNOWN")]
        groups, excluded = builder.stldc_stage_roster(rows, scope("X", "Y"), AS_OF)
        self.assertTrue(all(not values for values in groups.values()))
        self.assertEqual(excluded, ["X", "Y"])

    def test_empty_groups_explicit_and_confirm_not_flow_confirmation(self):
        groups, excluded = builder.stldc_stage_roster([], {}, AS_OF)
        result = builder.render_stldc_roster(groups, excluded, AS_OF)
        self.assertEqual(result.count("None / 无"), 4)
        self.assertIn("not option-flow verdicts", result)
        self.assertIn(AS_OF, result)

    def test_rendering_count_and_complete_tickers(self):
        groups, excluded = builder.stldc_stage_roster(
            [row("B", "BULL-ALERT"), row("A", "BULL-ALERT"), row("BAD", "BEAR-ALERT")],
            scope("A", "B"), AS_OF)
        result = builder.render_stldc_roster(groups, excluded, AS_OF)
        self.assertIn("<td>BULL-ALERT</td>\n<td>2</td>\n<td>A, B</td>", result)
        self.assertIn("Scope/QA exclusions: 1", result)
        self.assertEqual(result.count("<tr>"), 5)
        self.assertEqual(result.count("<td>"), 15)
        self.assertNotIn("<th>", result)
        self.assertNotIn("<details>", result)

    def test_no_top_n_truncation(self):
        symbols = [f"TEST{i:04d}" for i in range(1200)]
        groups, excluded = builder.stldc_stage_roster(
            [row(s, "BEAR-CONFIRM") for s in symbols], scope(*symbols), AS_OF)
        self.assertEqual(groups["BEAR-CONFIRM"], symbols)
        self.assertEqual(excluded, [])
        result = builder.render_stldc_roster(groups, excluded, AS_OF)
        self.assertIn("TEST1199", result)
        self.assertIn("<td>1200</td>", result)


if __name__ == "__main__":
    unittest.main()
