"""Offline tests for regime_score.py (no network). Run:
    python3 skills/daily-trading-briefing/tests/test_regime_score.py
"""
import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import regime_score as rs  # noqa: E402


def _series(n, start, drift, wiggle=0.004):
    d0 = date(2025, 1, 1)
    out, px = [], start
    for i in range(n):
        px *= 1 + drift + wiggle * math.sin(i / 3.0)
        out.append([(d0 + timedelta(days=i)).isoformat(), px])
    return out


def _inputs(drift=0.001, vix=14.0, vix3m=17.0, tide=150.0, bull=70, bear=30, frag="NORMAL"):
    n = 320
    spy = _series(n, 500, drift)
    return {
        "prices": {
            "SPY": spy,
            "QQQ": _series(n, 400, drift),
            "^VIX": [[d, vix + 2 * math.sin(i / 9)] for i, (d, _) in enumerate(spy)],
            "^VIX3M": [[d, vix3m] for d, _ in spy],
        },
        "tide": [{"date": f"2026-10-{20 + i:02d}", "avg_mm": tide} for i in range(6)],
        "stldc": [
            {"run_date": f"2026-10-{20 + i:02d}", "bull_confirm": bull, "bear_confirm": bear, "bull_alert": bull, "bear_alert": bear, "spy_regime": "BULL"}
            for i in range(4)
        ],
        "jlaw": [
            {"run_date": f"2026-10-{1 + i:02d}", "review_type": "chart", "valid": 3, "watch": 100 + i, "skip": 50}
            for i in range(10)
        ]
        + [{"run_date": "2026-10-10", "review_type": "yahoo", "valid": 2, "watch": 5, "skip": 200}],
        "fragility": {"lpi_tier": frag, "funding_legs": 0},
    }


class TideParse(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rs.parse_tide_avg_mm("all 39 buckets negative, avg $-547M net call premium"), -547.0)
        self.assertEqual(rs.parse_tide_avg_mm("3/39 negative, avg $45M net"), 45.0)
        self.assertEqual(rs.parse_tide_avg_mm("avg -$1.2B"), -1200.0)
        self.assertIsNone(rs.parse_tide_avg_mm("no number"))
        self.assertIsNone(rs.parse_tide_avg_mm(None))


class Bands(unittest.TestCase):
    def test_band_edges(self):
        self.assertEqual(rs.band_for(70), "RISK_ON")
        self.assertEqual(rs.band_for(69.9), "CONSTRUCTIVE")
        self.assertEqual(rs.band_for(45), "NEUTRAL")
        self.assertEqual(rs.band_for(29.9), "DEFENSIVE")

    def test_hysteresis_holds_near_boundary(self):
        hist = [{"band": "NEUTRAL", "score": 50}, {"band": "NEUTRAL", "score": 53}, {"band": "CONSTRUCTIVE", "score": 56}]
        band, note = rs.apply_hysteresis(hist)
        self.assertEqual(band, "NEUTRAL")
        self.assertIn("pending", note)

    def test_hysteresis_switches_far_past_boundary(self):
        hist = [{"band": "NEUTRAL", "score": 50}, {"band": "NEUTRAL", "score": 53}, {"band": "CONSTRUCTIVE", "score": 62}]
        self.assertEqual(rs.apply_hysteresis(hist)[0], "CONSTRUCTIVE")

    def test_hysteresis_downgrade(self):
        hist = [{"band": "NEUTRAL", "score": 50}, {"band": "NEUTRAL", "score": 47}, {"band": "CAUTIOUS", "score": 44}]
        self.assertEqual(rs.apply_hysteresis(hist)[0], "NEUTRAL")
        hist[-1] = {"band": "CAUTIOUS", "score": 38}
        self.assertEqual(rs.apply_hysteresis(hist)[0], "CAUTIOUS")

    def test_confirmed_change(self):
        hist = [{"band": "NEUTRAL", "score": 50}, {"band": "CONSTRUCTIVE", "score": 56}, {"band": "CONSTRUCTIVE", "score": 56}]
        self.assertEqual(rs.apply_hysteresis(hist)[0], "CONSTRUCTIVE")


class Composite(unittest.TestCase):
    def test_bull_scores_higher_than_bear(self):
        bull = rs.compute_regime(_inputs(), "2026-10-29")
        bear = rs.compute_regime(
            _inputs(drift=-0.0015, vix=32, vix3m=28, tide=-400, bull=20, bear=80, frag="HIGH"), "2026-10-29"
        )
        self.assertGreater(bull["score"], 65)
        self.assertLess(bear["score"], 30)
        self.assertEqual(bear["band"], "DEFENSIVE")
        self.assertNotIn("WHEEL_CSP", bear["playbook"]["income_strategies"])

    def test_missing_pillar_reweights(self):
        inp = _inputs()
        inp["fragility"] = None
        inp["tide"] = []
        res = rs.compute_regime(inp, "2026-10-29")
        self.assertEqual(sorted(res["missing_pillars"]), ["flow", "stress"])
        self.assertTrue(0 <= res["score"] <= 100)
        md = rs.render_markdown(res)
        self.assertIn("缺失 missing", md)

    def test_markov_probs_sum_to_one(self):
        closes = [p for _, p in _series(300, 100, 0.001)]
        mk = rs.markov_next_probs(closes)
        self.assertAlmostEqual(sum(mk["p_next"].values()), 1.0, places=2)
        self.assertIn(mk["state"], {"BULL", "SIDEWAYS", "BEAR"})

    def test_divergence_narrow_leadership(self):
        inp = _inputs(bull=20, bear=80)
        inp["jlaw"] = [
            {"run_date": f"2026-10-{1 + i:02d}", "review_type": "chart", "valid": 0, "watch": 120 - 5 * i, "skip": 50}
            for i in range(10)
        ]
        res = rs.compute_regime(inp, "2026-10-29")
        self.assertTrue(any("Narrow leadership" in d for d in res["divergences"]))

    def test_markdown_has_notion_table(self):
        md = rs.render_markdown(rs.compute_regime(_inputs(), "2026-10-29"))
        self.assertIn('<table header-row="true">', md)
        self.assertIn("Playbook", md)


if __name__ == "__main__":
    unittest.main(verbosity=1)
