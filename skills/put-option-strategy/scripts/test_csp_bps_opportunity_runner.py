#!/usr/bin/env python3
"""Offline unit tests for revised CSP/BPS gates, economics, and confidence score."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from csp_bps_opportunity_core import ScreeningConfig, build_audit, markdown_report


AS_OF = date(2026, 10, 9)


def context(earnings: bool = True, classification: str = "Valid") -> dict:
    return {
        "symbol": "TEST",
        "exchange": "NASDAQ",
        "jlaw": {"classification": classification, "stop_loss": 95, "option_iv_rank": 45.0, "jlaw_score": 13, "option_iv30": 0.45, "option_rv20": 0.35},
        "gex": {"put_wall": 96, "regime": "positive", "snapshot_ts": "2026-10-09T21:00:00Z"},
        "live_gex": {"put_wall": 96},
        "quant": {"rank": 12, "n_active_rules": 5, "score_hist_freq": 0.60},
        "term_structure": {"status": "normal"},
        "flow_summary": {"oi_confirmation": "stable", "review_status": "clear", "adverse_near_strike": False},
        "earnings": {"date": "2026-11-20"} if earnings else {},
    }


def chain(include_long: bool = True, include_floor_breach: bool = True, csp_delta: float = -0.32) -> dict:
    states = [
        {"option_type": "put", "option_symbol": "TEST261016P00095000", "expires": "2026-10-16", "strike": 95, "delta": csp_delta, "open_interest": 500, "volume": 90, "bid": 1.40, "ask": 1.60, "iv": 0.40, "last_tape_time": "2026-10-09T20:00:00Z"},
        # $3.20 short bid - $1.00 long ask = $2.20 credit, 28.205% return on risk.
        {"option_type": "put", "option_symbol": "TEST261030P00094000", "expires": "2026-10-30", "strike": 94, "delta": -0.01, "open_interest": 600, "volume": 120, "bid": 3.20, "ask": 3.50, "iv": 0.42, "last_tape_time": "2026-10-09T20:00:00Z"},
    ]
    if include_long:
        states.append({"option_type": "put", "option_symbol": "TEST261030P00084000", "expires": "2026-10-30", "strike": 84, "delta": -0.22, "open_interest": 700, "volume": 80, "bid": 0.80, "ask": 1.00, "iv": 0.38, "last_tape_time": "2026-10-09T20:00:00Z"})
    else:
        states.append({"option_type": "put", "option_symbol": "TEST261030P00085000", "expires": "2026-10-30", "strike": 85, "delta": -0.25, "open_interest": 700, "volume": 80, "bid": 0.90, "ask": 1.10, "iv": 0.39, "last_tape_time": "2026-10-09T20:00:00Z"})
    if include_floor_breach:
        states.append({"option_type": "put", "option_symbol": "TEST261016P00096000", "expires": "2026-10-16", "strike": 96, "delta": -0.35, "open_interest": 500, "volume": 90, "bid": 1.80, "ask": 2.00, "iv": 0.41, "last_tape_time": "2026-10-09T20:00:00Z"})
    return {"price_data": {"price": 100}, "states": states}


class CspBpsOpportunityTests(unittest.TestCase):
    def audit(self, *, earnings: bool = True, include_long: bool = True, csp_delta: float = -0.32) -> dict:
        return build_audit([context(earnings)], {"TEST": chain(include_long, csp_delta=csp_delta)}, ScreeningConfig(), AS_OF)

    def test_qualifies_csp_and_default_ten_point_bps(self) -> None:
        audit = self.audit()
        csp = [item for item in audit["ranked_top20"] if item["strategy"] == "CSP"]
        bps = [item for item in audit["ranked_top20"] if item["strategy"] == "BPS"]
        self.assertEqual(len(csp), 1)
        self.assertEqual(len(bps), 1)
        self.assertEqual(csp[0]["short_put"]["strike"], 95.0)
        self.assertEqual(csp[0]["credit"], 140.0)
        self.assertEqual(csp[0]["collateral"], 9360.0)
        self.assertAlmostEqual(csp[0]["credit_to_collateral"], 140 / 9360, places=6)
        self.assertEqual(bps[0]["width"], 10.0)
        self.assertEqual(bps[0]["credit"], 220.0)
        self.assertEqual(bps[0]["max_loss"], 780.0)
        self.assertAlmostEqual(bps[0]["return_on_risk"], 220 / 780, places=6)
        self.assertEqual(csp[0]["candidate_status"], "ELIGIBLE")
        self.assertEqual(bps[0]["candidate_status"], "ELIGIBLE")
        self.assertEqual(audit["counts"]["ranked_top20"], 2)

    def test_csp_delta_is_strictly_less_than_negative_point_twenty(self) -> None:
        audit = self.audit(csp_delta=-0.20)
        self.assertEqual([item for item in audit["ranked_top20"] if item["strategy"] == "CSP"], [])
        reasons = [reason for record in audit["evaluated"][0]["rejections"] for reason in record.get("reasons", [])]
        self.assertIn("CSP_DELTA_NOT_STRICTLY_BELOW_THRESHOLD", reasons)

    def test_bps_has_no_delta_gate(self) -> None:
        payload = chain()
        payload["states"][1]["delta"] = -0.01
        audit = build_audit([context()], {"TEST": payload}, ScreeningConfig(), AS_OF)
        self.assertEqual(len([item for item in audit["ranked_top20"] if item["strategy"] == "BPS"]), 1)

    def test_bps_rejects_return_on_risk_outside_twenty_to_thirty_band(self) -> None:
        payload = chain()
        payload["states"][2]["bid"] = 0.40
        payload["states"][2]["ask"] = 0.50  # $2.70 credit / $7.30 risk = 36.986%
        audit = build_audit([context()], {"TEST": payload}, ScreeningConfig(), AS_OF)
        self.assertEqual([item for item in audit["ranked_top20"] if item["strategy"] == "BPS"], [])
        reasons = [reason for record in audit["evaluated"][0]["rejections"] for reason in record.get("reasons", [])]
        self.assertIn("BPS_RETURN_ON_RISK_OUTSIDE_20_TO_30_PCT_BAND", reasons)

    def test_rejects_strike_above_technical_safety_floor(self) -> None:
        audit = self.audit()
        reasons = [reason for record in audit["evaluated"][0]["rejections"] for reason in record.get("reasons", [])]
        self.assertIn("ABOVE_TECHNICAL_SAFETY_FLOOR", reasons)

    def test_bps_requires_exact_default_width_long_leg(self) -> None:
        audit = self.audit(include_long=False)
        self.assertEqual([item for item in audit["ranked_top20"] if item["strategy"] == "BPS"], [])
        reasons = [reason for record in audit["evaluated"][0]["rejections"] for reason in record.get("reasons", [])]
        self.assertIn("DEFAULT_WIDTH_LONG_PUT_MISSING", reasons)

    def test_earnings_unknown_blocks_candidates_by_default(self) -> None:
        audit = self.audit(earnings=False)
        self.assertEqual(audit["ranked_top20"], [])
        reasons = [reason for record in audit["evaluated"][0]["rejections"] for reason in record.get("reasons", [])]
        self.assertIn("EARNINGS_UNCONFIRMED", reasons)

    def test_markdown_is_concise_and_discloses_revised_policy(self) -> None:
        report = markdown_report(self.audit())
        self.assertIn("10-Point Bull Put Spread", report)
        self.assertIn("no delta gate", report)
        self.assertIn("20%–30%", report)
        self.assertIn("Valid or Watch", report)
        self.assertIn("Score is a rank only", report)
        self.assertIn("research and analysis only", report.lower())

    def test_watch_is_eligible_by_default_and_scores_are_not_threshold_filters(self) -> None:
        audit = build_audit([context(classification="Watch")], {"TEST": chain()}, ScreeningConfig(), AS_OF)
        self.assertEqual(len(audit["ranked_top20"]), 2)
        self.assertTrue(all(item["candidate_status"] == "ELIGIBLE" for item in audit["ranked_top20"]))

    def test_fixture_runner_writes_both_artifacts(self) -> None:
        fixture = {"as_of": AS_OF.isoformat(), "contexts": [context()], "chains": {"TEST": chain()}}
        script = Path(__file__).with_name("csp_bps_opportunity_runner.py")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture_path = tmp_path / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), "--fixture", str(fixture_path), "--output-dir", str(tmp_path / "out")], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((tmp_path / "out" / "csp_bps_opportunity_audit.json").is_file())
            report = (tmp_path / "out" / "csp_bps_opportunity_report.md").read_text(encoding="utf-8")
            self.assertIn("CSP & 10-Point Bull Put Spread — Top 20 Ranked Board", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
