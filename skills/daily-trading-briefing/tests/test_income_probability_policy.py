#!/usr/bin/env python3
"""Offline regression tests for exact EM joins and the soft 30/20/50 board.

All values are synthetic. These tests require no market data, account access,
network calls, database writes, or generated fixture files.
"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from map_em_probabilities import attach_probabilities, select_top20  # noqa: E402
import render_weekly_income_board as board_renderer  # noqa: E402


MODEL_AS_OF = "2026-11-03"
REPORT_DATE = "2026-11-03"
EXPIRY = "2026-11-13"
SOURCES = (
    "option-opportunity-davis",
    "csp-bps-opportunity",
    "uw-option-opportunities",
)
PUTS = {"SP", "CSP", "WHEEL_CSP"}
CALLS = {"CC", "WHEEL_CC", "COVERED_CALL"}
SPREADS = {"BPS", "BCS"}


def candidate(ticker="TEST", strategy="CSP", key=None, **overrides):
    row = {
        "ticker": ticker,
        "strategy": strategy,
        "source_table": SOURCES[0],
        "opportunity_key": key or f"{ticker}-{strategy}",
        "source_report_date": REPORT_DATE,
        "expiry": EXPIRY,
        "short_strike": 100.0,
        "exchange": "NASDAQ",
        "qty": 1,
        "eligible_for_ranking": True,
    }
    if strategy in CALLS:
        row.update(shares_available=200, share_ownership_verified=True, share_snapshot_as_of=MODEL_AS_OF)
    row.update(overrides)
    return row


def score_for(row, **overrides):
    score = {
        "source_table": row["source_table"],
        "opportunity_key": row["opportunity_key"],
        "report_date": row["source_report_date"],
        "symbol": row["ticker"],
        "strategy": row["strategy"],
        "strike": row["short_strike"],
        "expiry": row["expiry"],
        "option_type": "call" if row["strategy"] in CALLS | {"BCS"} else "put",
        "p_win_expiry": 0.76,
        "p_profit50_first": 0.61,
        "model_version": "em-offline-test-v1",
        "sigma_as_of": MODEL_AS_OF,
        "earnings_in_window": False,
        "calib_cov_30d": 0.80,
    }
    score.update(overrides)
    return score


def matched(rows, probabilities=None):
    """Use the actual mapper, not a fabricated match marker, before selection."""
    scores = []
    for index, row in enumerate(rows):
        overrides = {} if probabilities is None else probabilities[index]
        scores.append(score_for(row, **overrides))
    result = attach_probabilities(deepcopy(rows), scores, model_as_of=MODEL_AS_OF)
    if result["exclusions"]:
        raise AssertionError(f"Valid synthetic fixture was excluded: {result['exclusions']}")
    return result["candidates"]


def units(board):
    return sum(int(row.get("qty", 1)) for row in board)


def family_units(board):
    counts = Counter({"puts": 0, "calls": 0, "spreads": 0})
    for row in board:
        strategy = row["strategy"]
        family = "puts" if strategy in PUTS else "calls" if strategy in CALLS else "spreads"
        counts[family] += int(row.get("qty", 1))
    return counts


def board_identity(board):
    return [
        (
            row["ticker"], row["strategy"], row["source_table"],
            row["opportunity_key"], row["expiry"], row["short_strike"],
            int(row.get("qty", 1)),
        )
        for row in board
    ]


def pool(put_count=10, call_count=7, spread_count=15):
    rows = []
    for prefix, strategy, count in (
        ("PUT", "CSP", put_count), ("CALL", "CC", call_count),
        ("SPR", "BPS", spread_count),
    ):
        for index in range(count):
            rows.append(candidate(f"{prefix}{index:02d}", strategy))
    probabilities = [
        {"p_win_expiry": 0.95 - index * 0.005, "p_profit50_first": 0.70}
        for index in range(len(rows))
    ]
    return matched(rows, probabilities)


class ExactProbabilityJoinTests(unittest.TestCase):
    def assert_excluded(self, row, scores):
        result = attach_probabilities([deepcopy(row)], deepcopy(scores), model_as_of=MODEL_AS_OF)
        self.assertIsInstance(result["candidates"], list)
        self.assertIsInstance(result["exclusions"], list)
        self.assertTrue(result["exclusions"], "Missing/invalid scores must be auditable exclusions")
        for returned in result["candidates"]:
            probability = returned.get("em_probability")
            self.assertFalse(
                isinstance(probability, dict) and probability.get("match_status") == "matched",
                "A rejected score must never be represented as an exact valid match",
            )
        selected = select_top20(result["candidates"])
        self.assertEqual(selected["board"], [], "Rejected probabilities cannot enter ranking")
        return result

    def test_exact_join_preserves_every_score_field_and_correct_metric(self):
        row = candidate()
        score = score_for(row)
        result = attach_probabilities([row], [score], model_as_of=MODEL_AS_OF)
        self.assertEqual(result["exclusions"], [])
        self.assertEqual(len(result["candidates"]), 1)
        probability = result["candidates"][0]["em_probability"]
        for key, value in score.items():
            with self.subTest(field=key):
                self.assertEqual(probability[key], value)
        self.assertEqual(probability["match_status"], "matched")
        self.assertEqual(probability["metric_label"], "P(short leg OTM at expiry)")

    def test_all_three_exact_source_tables_are_supported(self):
        for source in SOURCES:
            with self.subTest(source=source):
                row = candidate(source_table=source)
                result = attach_probabilities([row], [score_for(row)], model_as_of=MODEL_AS_OF)
                self.assertEqual(result["exclusions"], [])
                self.assertEqual(result["candidates"][0]["em_probability"]["source_table"], source)

    def test_wrong_source_same_ticker_and_key_cannot_transfer_score(self):
        row = candidate(source_table=SOURCES[0])
        self.assert_excluded(row, [score_for(row, source_table=SOURCES[1])])

    def test_each_candidate_receives_its_own_source_specific_probability(self):
        rows = [candidate(source_table=source, key="shared-key") for source in SOURCES]
        scores = [score_for(row, p_win_expiry=0.60 + index * 0.10) for index, row in enumerate(rows)]
        result = attach_probabilities(rows, list(reversed(scores)), model_as_of=MODEL_AS_OF)
        self.assertEqual(result["exclusions"], [])
        expected = {score["source_table"]: score["p_win_expiry"] for score in scores}
        self.assertEqual(len(result["candidates"]), 3)
        for row in result["candidates"]:
            self.assertEqual(row["em_probability"]["p_win_expiry"], expected[row["source_table"]])

    def test_unknown_or_near_match_source_names_are_rejected(self):
        for source in ("unknown-table", "csp_bps_opportunity", "csp-bps-opportunity-extra"):
            with self.subTest(source=source):
                row = candidate(source_table=source)
                self.assert_excluded(row, [score_for(row)])

    def test_changed_opportunity_key_is_not_a_ticker_fallback(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, opportunity_key="other-ticket")])

    def test_changed_symbol_is_not_an_exact_match(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, symbol="OTHER")])

    def test_changed_strategy_is_not_an_exact_match(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, strategy="WHEEL_CSP")])

    def test_covered_call_alias_cannot_transfer_a_different_strategy_score(self):
        row = candidate(strategy="WHEEL_CC")
        self.assert_excluded(row, [score_for(row, strategy="CC")])

    def test_duplicate_source_key_scores_are_excluded_as_ambiguous(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row), score_for(row, p_win_expiry=0.90)])

    def test_changed_short_strike_is_not_an_exact_match(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, strike=105.0)])

    def test_changed_expiry_is_not_an_exact_match(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, expiry="2026-11-20")])

    def test_changed_report_date_is_not_an_exact_match(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, report_date="2026-11-02")])

    def test_model_version_must_be_present_and_nonempty(self):
        row = candidate()
        for version in (None, "", "   "):
            with self.subTest(model_version=version):
                self.assert_excluded(row, [score_for(row, model_version=version)])
        score = score_for(row)
        del score["model_version"]
        self.assert_excluded(row, [score])

    def test_stale_future_and_missing_sigma_are_rejected(self):
        row = candidate()
        for sigma_as_of in ("2026-11-02", "2026-11-04", None, ""):
            with self.subTest(sigma_as_of=sigma_as_of):
                self.assert_excluded(row, [score_for(row, sigma_as_of=sigma_as_of)])

    def test_model_session_argument_controls_freshness_not_wall_clock(self):
        row = candidate()
        score = score_for(row, sigma_as_of="2026-10-30")
        result = attach_probabilities([row], [score], model_as_of="2026-10-30")
        self.assertEqual(result["exclusions"], [])
        self.assertEqual(result["candidates"][0]["em_probability"]["sigma_as_of"], "2026-10-30")

    def test_earnings_in_model_window_is_not_usable_for_ranking(self):
        row = candidate()
        self.assert_excluded(row, [score_for(row, earnings_in_window=True)])

    def test_missing_score_never_generates_a_zero_or_estimate(self):
        row = candidate()
        result = self.assert_excluded(row, [])
        for returned in result["candidates"]:
            probability = returned.get("em_probability")
            if isinstance(probability, dict):
                self.assertIsNone(probability.get("p_win_expiry"))
                self.assertIsNone(probability.get("p_profit50_first"))

    def test_missing_probability_fields_are_excluded_not_filled(self):
        row = candidate()
        for field in ("p_win_expiry", "p_profit50_first"):
            with self.subTest(field=field):
                score = score_for(row)
                del score[field]
                self.assert_excluded(row, [score])

    def test_non_fraction_and_non_finite_probabilities_are_invalid(self):
        row = candidate()
        for field in ("p_win_expiry", "p_profit50_first", "calib_cov_30d"):
            for value in (-0.001, 1.001, None, "not-a-probability", float("nan"), float("inf")):
                if value is None and field != "p_win_expiry":
                    continue  # These two fields are nullable in the actual EM schema.
                with self.subTest(field=field, value=value):
                    self.assert_excluded(row, [score_for(row, **{field: value})])

    def test_nullable_secondary_metrics_stay_null_without_overriding_primary_measurement(self):
        row = candidate()
        score = score_for(row, p_profit50_first=None, calib_cov_30d=None)
        result = attach_probabilities([row], [score], model_as_of=MODEL_AS_OF)
        self.assertEqual(result["exclusions"], [])
        em = result["candidates"][0]["em_probability"]
        self.assertIsNone(em["p_profit50_first"])
        self.assertIsNone(em["calib_cov_30d"])
        self.assertIn("skepticism", em["calibration_note"])
        self.assertEqual(em["p_win_expiry"], score["p_win_expiry"])
        self.assertEqual(units(select_top20(result["candidates"])["board"]), 1)

    def test_real_zero_and_one_probabilities_are_not_treated_as_missing(self):
        row = candidate()
        for probability in (0.0, 1.0):
            with self.subTest(probability=probability):
                score = score_for(row, p_win_expiry=probability, p_profit50_first=probability)
                result = attach_probabilities([row], [score], model_as_of=MODEL_AS_OF)
                self.assertEqual(result["exclusions"], [])
                self.assertEqual(result["candidates"][0]["em_probability"]["p_win_expiry"], probability)


class RankedIncomeSelectionTests(unittest.TestCase):
    def assert_result_shape(self, result):
        self.assertIsInstance(result["board"], list)
        self.assertIsInstance(result["allocation"], dict)
        self.assertIsInstance(result["deviations"], list)
        actual = family_units(result["board"])
        self.assertEqual(result["allocation"], {
            "single_put": actual["puts"],
            "covered_call": actual["calls"],
            "spread": actual["spreads"],
        })

    def test_full_pool_uses_six_put_four_call_ten_spread_soft_mix(self):
        result = select_top20(list(reversed(pool())))
        self.assert_result_shape(result)
        self.assertEqual(units(result["board"]), 20)
        self.assertEqual(family_units(result["board"]), {"puts": 6, "calls": 4, "spreads": 10})
        self.assertEqual(result["deviations"], [])
        self.assertTrue(result["allocation"], "Actual allocation must be reported")
        for row in result["board"]:
            self.assertEqual(row["em_probability"]["match_status"], "matched")

    def test_selected_rows_are_probability_descending_even_with_soft_mix(self):
        result = select_top20(list(reversed(pool())))
        board = result["board"]
        ordering = [
            (row["em_probability"]["p_win_expiry"], row["em_probability"]["p_profit50_first"])
            for row in board
        ]
        self.assertEqual(ordering, sorted(ordering, reverse=True))
        self.assertEqual(len(board), 20)

    def test_each_family_selects_its_highest_probability_available_rows(self):
        rows = pool()
        result = select_top20(list(reversed(rows)))
        for strategies, target in ((PUTS, 6), (CALLS, 4), (SPREADS, 10)):
            expected = sorted(
                (row for row in rows if row["strategy"] in strategies),
                key=lambda row: row["em_probability"]["p_win_expiry"], reverse=True,
            )[:target]
            actual = [row for row in result["board"] if row["strategy"] in strategies]
            self.assertEqual({row["opportunity_key"] for row in actual}, {row["opportunity_key"] for row in expected})

    def test_probability_tie_uses_profit50_before_deterministic_identity(self):
        rows = [candidate("AAA"), candidate("ZZZ"), candidate("MMM")]
        rows = matched(rows, [
            {"p_win_expiry": 0.80, "p_profit50_first": 0.40},
            {"p_win_expiry": 0.80, "p_profit50_first": 0.70},
            {"p_win_expiry": 0.90, "p_profit50_first": 0.10},
        ])
        board = select_top20(rows)["board"]
        self.assertEqual([row["ticker"] for row in board], ["MMM", "ZZZ", "AAA"])

    def test_equal_probability_identity_order_does_not_depend_on_input_order(self):
        rows = matched([candidate("ZZZ"), candidate("AAA"), candidate("MMM")])
        expected = board_identity(select_top20(rows)["board"])
        for reordered in (list(reversed(rows)), rows[1:] + rows[:1]):
            self.assertEqual(board_identity(select_top20(reordered)["board"]), expected)

    def test_insufficient_covered_call_pool_fills_surplus_and_reports_actual_deviation(self):
        rows = pool(put_count=10, call_count=2, spread_count=15)
        result = select_top20(rows)
        self.assert_result_shape(result)
        counts = family_units(result["board"])
        self.assertEqual(units(result["board"]), 20)
        self.assertEqual(counts["calls"], 2)
        self.assertGreaterEqual(counts["puts"], 6)
        self.assertGreaterEqual(counts["spreads"], 10)
        self.assertTrue(result["deviations"], "A two-unit CC shortfall must be disclosed")
        self.assertTrue(any(
            deviation.get("family") == "covered_call"
            and deviation.get("target") == 4 and deviation.get("actual") == 2
            for deviation in result["deviations"]
        ), "Deviation must disclose the actual CC supply, not the nominal target")
        self.assertTrue(result["allocation"])
        available = {row["opportunity_key"] for row in rows}
        self.assertTrue(all(row["opportunity_key"] in available for row in result["board"]))

    def test_small_available_pool_stays_small_without_fabricated_units(self):
        rows = pool(put_count=2, call_count=1, spread_count=3)
        result = select_top20(rows)
        self.assertEqual(units(result["board"]), 6)
        self.assertEqual(family_units(result["board"]), {"puts": 2, "calls": 1, "spreads": 3})
        self.assertTrue(result["deviations"])
        self.assertEqual({row["opportunity_key"] for row in result["board"]}, {row["opportunity_key"] for row in rows})

    def test_empty_pool_returns_empty_board_and_reports_shortfall(self):
        result = select_top20([])
        self.assert_result_shape(result)
        self.assertEqual(result["board"], [])
        self.assertEqual(units(result["board"]), 0)
        self.assertTrue(result["deviations"])

    def test_more_than_twenty_available_units_does_not_select_more_than_twenty(self):
        rows = pool(put_count=25, call_count=25, spread_count=25)
        result = select_top20(rows)
        self.assertEqual(units(result["board"]), 20)
        self.assertLessEqual(len(result["board"]), 20)
        self.assertEqual(family_units(result["board"]), {"puts": 6, "calls": 4, "spreads": 10})

    def test_ineligible_or_missing_eligibility_cannot_rank(self):
        for eligibility in (False, None, "true"):
            with self.subTest(eligibility=eligibility):
                row = candidate(eligible_for_ranking=eligibility)
                self.assertEqual(select_top20(matched([row]))["board"], [])
        row = candidate()
        del row["eligible_for_ranking"]
        self.assertEqual(select_top20(matched([row]))["board"], [])

    def test_missing_or_unmatched_model_record_cannot_rank(self):
        row = candidate()
        self.assertEqual(select_top20([row])["board"], [])
        mapped = matched([row])[0]
        mapped["em_probability"]["match_status"] = "missing"
        self.assertEqual(select_top20([mapped])["board"], [])

    def test_supported_put_call_and_spread_strategy_aliases_are_selectable(self):
        for strategy in sorted(PUTS | CALLS | SPREADS):
            with self.subTest(strategy=strategy):
                result = select_top20(matched([candidate(strategy=strategy)]))
                self.assertEqual(units(result["board"]), 1)
                self.assertEqual(result["board"][0]["strategy"], strategy)

    def test_non_us_or_unknown_exchange_is_not_ranked(self):
        for exchange in ("LSE", "HKEX", "TSX", "", None):
            with self.subTest(exchange=exchange):
                self.assertEqual(select_top20(matched([candidate(exchange=exchange)]))["board"], [])

    def test_supported_us_exchange_names_are_selectable(self):
        for exchange in ("NASDAQ", "NYSE", "AMEX", "NYSEARCA", "NYSEAMERICAN", "CBOE", "BATS"):
            with self.subTest(exchange=exchange):
                result = select_top20(matched([candidate(exchange=exchange)]))
                self.assertEqual(units(result["board"]), 1)

    def test_quantity_defaults_to_one_when_not_supplied(self):
        row = candidate()
        del row["qty"]
        result = select_top20(matched([row]))
        self.assertEqual(units(result["board"]), 1)

    def test_two_unit_ticket_counts_as_two_strategy_units_not_one_row(self):
        rows = matched([candidate("DOUBLE", qty=2), candidate("SINGLE")])
        result = select_top20(rows)
        self.assertEqual(units(result["board"]), 3)
        self.assertEqual(family_units(result["board"])["puts"], 3)

    def test_ticker_concentration_is_aggregated_across_strategies_and_tickets(self):
        rows = matched([
            candidate("SAME", "CSP", key="put-a", qty=2),
            candidate("SAME", "CC", key="call-a", short_strike=105.0),
            candidate("SAME", "BPS", key="spread-a", short_strike=95.0),
            candidate("OTHER", "BPS"),
        ])
        result = select_top20(rows)
        counts = Counter()
        for row in result["board"]:
            counts[row["ticker"]] += int(row.get("qty", 1))
        self.assertTrue(counts)
        self.assertLessEqual(counts["SAME"], 2)
        self.assertTrue(all(count <= 2 for count in counts.values()))

    def test_exact_ticket_limit_is_not_bypassed_by_duplicate_source_rows(self):
        rows = [candidate("DUP", key=f"duplicate-{index}", source_table=source, qty=2)
                for index, source in enumerate(SOURCES)]
        result = select_top20(matched(rows))
        self.assertLessEqual(units(result["board"]), 2)
        self.assertTrue(result["board"], "At least one genuinely available two-unit ticket remains")

    def test_overlarge_requested_ticket_never_exceeds_two_selected_units(self):
        result = select_top20(matched([candidate(qty=3)]))
        self.assertLessEqual(units(result["board"]), 2)

    def test_covered_calls_require_literal_verified_ownership(self):
        for verified in (False, None, "true"):
            with self.subTest(verified=verified):
                row = candidate(strategy="CC", share_ownership_verified=verified, shares_available=1000)
                self.assertEqual(select_top20(matched([row]))["board"], [])
        row = candidate(strategy="CC")
        del row["share_ownership_verified"]
        self.assertEqual(select_top20(matched([row]))["board"], [])

    def test_covered_calls_require_dated_share_snapshot(self):
        row = candidate(strategy="CC")
        del row["share_snapshot_as_of"]
        self.assertEqual(select_top20(matched([row]))["board"], [])

    def test_covered_call_share_capacity_uses_hundred_share_multiplier(self):
        for quantity, available, expected in ((1, 99, 0), (1, 100, 1), (2, 199, None), (2, 200, 2)):
            with self.subTest(quantity=quantity, available=available):
                row = candidate(strategy="CC", qty=quantity, shares_available=available)
                board = select_top20(matched([row]))["board"]
                if expected is not None:
                    self.assertEqual(units(board), expected)
                self.assertLessEqual(units(board) * 100, available)

    def test_covered_call_share_capacity_is_shared_across_same_ticker_tickets(self):
        rows = matched([
            candidate("OWNER", "CC", key="first-call", shares_available=100),
            candidate("OWNER", "WHEEL_CC", key="second-call", short_strike=105.0, shares_available=100),
        ])
        result = select_top20(rows)
        self.assertLessEqual(units(result["board"]), 1)


class RankedIncomeRendererTests(unittest.TestCase):
    def test_qualified_csp_is_not_blanket_blocked_in_weak_regime(self):
        row = matched([candidate("WEAK", "CSP")])[0]
        row.update(
            rank=1, contract="short WEAK synthetic put", dte=10,
            short_bid=1.25, short_ask=1.35, reference_credit_per_contract=125,
            collateral_per_contract=10000, delta=-0.25, short_oi=1000,
            short_volume=100, earnings="No event in synthetic fixture",
            confidence="Medium", source="Synthetic offline test only",
            flow="neutral synthetic evidence", stldc="synthetic screen",
            dark_pool="synthetic context", gate="Not a trading instruction",
        )
        plan_fields = (
            "week", "date_range", "catalysts", "book_action", "re_strike_queue",
            "conditions_and_vetoes", "strategies", "credit_plan",
        )
        for regime in ("CAUTIOUS", "DEFENSIVE"):
            with self.subTest(regime=regime):
                manifest = {
                    "as_of": MODEL_AS_OF,
                    "model_as_of": MODEL_AS_OF,
                    "quote_basis": "Synthetic dated fixture; not a current quote",
                    "effective_regime": regime,
                    "board_rule": {
                        "selection_mode": "ranked_30_20_50",
                        "total_contracts": 20, "max_contracts_per_ticker": 2,
                        "single_put_contracts": 6, "covered_call_contracts": 4,
                        "spread_contracts": 10, "csp_dte_cap": 35, "spread_dte_cap": 45,
                    },
                    "board": [deepcopy(row)],
                    "four_week_plan": [
                        {field: f"Synthetic week {index}: {field}" for field in plan_fields}
                        for index in range(1, 5)
                    ],
                    "portfolio_context": {
                        "snapshot_as_of": MODEL_AS_OF, "summary": "Synthetic test only",
                        "triage_rule": "Review risk", "credit_separation": "No live P&L",
                    },
                }
                validated, totals = board_renderer.validate(manifest)
                self.assertEqual(len(validated), 1)
                self.assertEqual(totals["single_put_contracts"], 1)
                self.assertEqual(totals["contracts"], 1)
                rendered = board_renderer.render(manifest)
                self.assertIn("WEAK", rendered)
                self.assertNotIn("blocks new CSP", rendered)


if __name__ == "__main__":
    unittest.main()
