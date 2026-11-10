#!/usr/bin/env python3
"""Offline unit tests for uw_option_income2_runner.py — no network, no Supabase.

Run:  python tests/test_uw_option_income2_runner.py
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from uw_option_income2_runner import (  # noqa: E402
    BEAR_SIDE_STRATEGIES,
    BULL_SIDE_STRATEGIES,
    DTE_BUCKETS,
    SINGLE_LEG_STRATEGIES,
    SPREAD_MIN_CREDIT,
    BucketWalls,
    OptionContractQuote,
    SymbolSignalBundle,
    UWValuationContext,
    UWVolatilityContext,
    aggregate_sector_health,
    build_watchlist_universe,
    classify_market_regime,
    compute_bucket_walls,
    compute_flow_composite,
    evaluate_open_opportunity_row,
    flow_label_for,
    generate_opportunities_for_symbol,
    is_earnings_clear,
    is_nontech_row,
    iso_week_key,
    plan_weekly_quota,
    regime_tilt,
    sector_blocks_side,
)

REPORT_DATE = date(2026, 11, 7)  # Saturday; expiries 11-13 (DTE 6) and 11-20 (DTE 13)


def mk_quote(
    symbol="TEST",
    cp="P",
    expiry="2026-11-13",
    dte=6,
    strike=95.0,
    bid=1.00,
    ask=1.10,
    delta=-0.15,
    gamma=0.05,
    oi=2000,
):
    ask = max(ask, bid + 0.05)  # keep NBBO coherent even when tests raise the bid
    return OptionContractQuote(
        option_symbol=f"{symbol}{expiry.replace('-', '')[2:]}{cp}{int(strike * 1000):08d}",
        underlying_symbol=symbol,
        option_type="put" if cp == "P" else "call",
        expiry=expiry,
        dte=dte,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=gamma,
        iv=0.45,
        open_interest=oi,
    )


def mk_bundle(
    symbol="TEST",
    spot=100.0,
    category="core",
    sector="Semiconductor",
    verdict="bullish",
    iv_rank=40.0,
    rsi=50.0,
    stop_loss=None,
    dp_accumulating=True,
):
    b = SymbolSignalBundle(
        symbol=symbol,
        watchlist_category=category,
        spot_price=spot,
        sector=sector,
        verdict=verdict,
        verdict_date=REPORT_DATE.isoformat(),
        jlaw_classification="valid",
        jlaw_score=12,
        jlaw_stop_loss=stop_loss,
        dp_accumulating=dp_accumulating,
    )
    b.volatility = UWVolatilityContext(symbol=symbol, iv_rank=iv_rank, rsi_14=rsi, iv30=0.45, iv7=0.48)
    b.valuation = UWValuationContext(symbol=symbol, ttm_eps=2.0, pe_ratio=50.0, valuation_tier="fair_low")
    return b


def mk_walls(bucket="LT8", put=95.0, call=110.0):
    return BucketWalls(bucket=bucket, put_wall=put, call_wall=call, source="bucket_chain")


# ---------------------------------------------------------------------------
# Bucket walls (term structure)
# ---------------------------------------------------------------------------


class TestBucketWalls(unittest.TestCase):
    def setUp(self):
        self.chain = [
            mk_quote(strike=95.0, dte=6, gamma=0.05, oi=2000),          # LT8 put wall
            mk_quote(strike=90.0, dte=6, gamma=0.04, oi=500),
            mk_quote(strike=95.0, dte=13, gamma=0.06, oi=300, expiry="2026-11-20"),
            mk_quote(strike=88.0, dte=13, gamma=0.03, oi=3000, expiry="2026-11-20"),  # DTE8_14 put wall
            mk_quote(cp="C", strike=110.0, dte=6, gamma=0.03, oi=1500, delta=0.12),
            mk_quote(cp="C", strike=115.0, dte=13, gamma=0.02, oi=100, delta=0.08, expiry="2026-11-20"),
        ]

    def test_term_structure_walls_differ_by_bucket(self):
        w6 = compute_bucket_walls(self.chain, 100.0, "LT8")
        w13 = compute_bucket_walls(self.chain, 100.0, "DTE8_14")
        self.assertEqual(w6.put_wall, 95.0)
        self.assertEqual(w6.call_wall, 110.0)
        self.assertEqual(w13.put_wall, 88.0)      # nearer put wall in the near bucket
        self.assertEqual(w13.call_wall, 115.0)
        self.assertEqual(w6.source, "bucket_chain")

    def test_dte_window_respected(self):
        w = compute_bucket_walls(self.chain, 100.0, "LT8")
        # 11-20 expiry (DTE 13) must not influence the LT8 bucket
        self.assertNotEqual(w.put_wall, 88.0)

    def test_fallback_to_gex_blend(self):
        empty = []
        w = compute_bucket_walls(empty, 100.0, "LT8", gex_blend_put_wall=93.0, gex_blend_call_wall=112.0)
        self.assertEqual(w.source, "gex_blend")
        self.assertEqual(w.put_wall, 93.0)
        self.assertEqual(w.call_wall, 112.0)

    def test_no_wall_when_nothing_available(self):
        w = compute_bucket_walls([], 100.0, "LT8")
        self.assertEqual(w.source, "none")
        self.assertIsNone(w.put_wall)

    def test_walls_sit_on_correct_side_of_spot(self):
        w = compute_bucket_walls(self.chain, 100.0, "LT8")
        self.assertLessEqual(w.put_wall, 100.0)
        self.assertGreaterEqual(w.call_wall, 100.0)


# ---------------------------------------------------------------------------
# Strategy generation gates
# ---------------------------------------------------------------------------


class TestStrategyGeneration(unittest.TestCase):
    def _gen(self, chain, bundle=None, walls=None, sector_health=None, trades=None):
        bundle = bundle or mk_bundle()
        walls = walls or {"LT8": mk_walls("LT8"), "DTE8_14": mk_walls("DTE8_14")}
        return generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls,
            option_trades=trades, sector_health=sector_health or {}, market_regime="RISK_ON",
        )

    def test_short_put_anchored_below_bucket_put_wall(self):
        chain = [
            mk_quote(strike=95.0, delta=-0.15, bid=0.90),   # == wall 95
            mk_quote(strike=97.0, delta=-0.18, bid=1.20),   # > wall, must be rejected
        ]
        rows, _ = self._gen(chain)
        sp = [r for r in rows if r["strategy"] == "SHORT_PUT"]
        self.assertTrue(sp)
        self.assertLessEqual(sp[0]["short_strike"], 95.0)

    def test_tighter_jlaw_stop_binds_below_wall(self):
        bundle = mk_bundle(stop_loss=92.0)                   # anchor = min(wall 95, stop 92)
        chain = [mk_quote(strike=94.0, delta=-0.15, bid=0.90)]  # <= wall but > stop
        rows, _ = self._gen(chain, bundle=bundle)
        self.assertFalse([r for r in rows if r["strategy"] == "SHORT_PUT"])

    def test_spread_credit_gate_above_70c(self):
        # 5-wide BPS: short 95 bid 1.80 / long 90 ask 1.05 -> credit 0.75 (passes)
        chain = [
            mk_quote(strike=95.0, delta=-0.18, bid=1.80),
            mk_quote(strike=90.0, delta=-0.10, bid=0.95, ask=1.05),
            mk_quote(strike=96.0, delta=-0.20, bid=1.70),
            mk_quote(strike=91.0, delta=-0.11, bid=0.90, ask=1.05),  # credit 0.65 (fails)
        ]
        rows, _ = self._gen(chain)
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertTrue(bps)
        for r in bps:
            self.assertGreater(r["entry_credit"], SPREAD_MIN_CREDIT)

    def test_spread_credit_exactly_70_fails(self):
        chain = [
            mk_quote(strike=95.0, delta=-0.18, bid=1.75),
            mk_quote(strike=90.0, delta=-0.10, bid=0.95, ask=1.05),  # credit exactly 0.70 -> reject (strict >)
        ]
        rows, _ = self._gen(chain)
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertFalse(bps)

    def test_spread_credit_float_precision_70_fails(self):
        # 2.20 - 1.50 = 0.7000000000000002 in float64; must NOT slip past the strict > $0.70 gate
        chain = [
            mk_quote(strike=95.0, delta=-0.18, bid=2.20),
            mk_quote(strike=90.0, delta=-0.10, bid=1.40, ask=1.50),
        ]
        rows, _ = self._gen(chain)
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertFalse(bps)

    def test_five_wide_preferred_even_above_150(self):
        # spot 400: legacy rule chose $10 width; v2 must prefer the $5 vertical when it qualifies
        bundle = mk_bundle(spot=400.0)
        chain = [
            mk_quote(strike=380.0, delta=-0.18, bid=3.00),           # 5-wide: long 375
            mk_quote(strike=375.0, delta=-0.10, bid=2.00, ask=2.10),
            mk_quote(strike=370.0, delta=-0.08, bid=1.60, ask=1.70),  # 10-wide long
        ]
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=380.0, call_wall=430.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=380.0, call_wall=430.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertTrue(bps)
        self.assertEqual(bps[0]["spread_width"], 5.0)
        self.assertEqual(bps[0]["leg2_strike"], 375.0)

    def test_ten_wide_only_as_fallback(self):
        # only the 10-wide vertical clears the credit + RoR gates -> width 10 used
        bundle = mk_bundle(spot=400.0)
        chain = [
            mk_quote(strike=380.0, delta=-0.18, bid=2.00),            # short candidate
            mk_quote(strike=375.0, delta=-0.05, bid=1.50, ask=1.60),  # delta out of band; 5-wide credit 0.40 fails
            mk_quote(strike=370.0, delta=-0.08, bid=0.50, ask=0.60),  # 10-wide credit 1.40, RoR 0.16 -> passes
        ]
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=380.0, call_wall=430.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=380.0, call_wall=430.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertTrue(bps)
        self.assertEqual(bps[0]["spread_width"], 10.0)

    def test_strong_bear_flow_vetoes_bull_side(self):
        bundle = mk_bundle()
        compute_flow_composite(bundle, REPORT_DATE)  # ensure initialized
        bundle.flow_label = "strong_bear"
        bundle.flow_composite = -55.0
        chain = [mk_quote(strike=95.0, delta=-0.15, bid=0.90)]
        rows, vetoes = self._gen(chain, bundle=bundle)
        self.assertFalse([r for r in rows if r["strategy"] in BULL_SIDE_STRATEGIES])
        self.assertTrue(any("SHORT_PUT:STRONG_BEAR_FLOW" in v for v in vetoes))

    def test_strong_bull_flow_vetoes_bear_side(self):
        bundle = mk_bundle(verdict="bearish", rsi=70.0)
        bundle.flow_label = "strong_bull"
        bundle.flow_composite = 55.0
        chain = [
            mk_quote(cp="C", strike=112.0, delta=0.14, bid=0.80),
            mk_quote(cp="C", strike=117.0, delta=0.08, bid=0.45, ask=0.55),
        ]
        rows, vetoes = self._gen(chain, bundle=bundle)
        self.assertFalse([r for r in rows if r["strategy"] in BEAR_SIDE_STRATEGIES])
        self.assertTrue(any("SHORT_CALL:STRONG_BULL_FLOW" in v for v in vetoes))

    def test_short_call_requires_ceiling(self):
        bundle = mk_bundle(verdict="bearish", rsi=65.0, dp_accumulating=False)
        chain = [
            mk_quote(cp="C", strike=105.0, delta=0.12, bid=1.50),  # below call wall 110 -> reject
            mk_quote(cp="C", strike=111.0, delta=0.10, bid=0.85),  # above wall & spot*1.03
        ]
        rows, _ = self._gen(chain, bundle=bundle)
        sc = [r for r in rows if r["strategy"] == "SHORT_CALL"]
        self.assertTrue(sc)
        self.assertGreaterEqual(sc[0]["short_strike"], 110.0)
        self.assertEqual(sc[0]["risk_flag"], "UNDEFINED_RISK")

    def test_earnings_blackout_blocks_all(self):
        bundle = mk_bundle()
        bundle.valuation.earnings_date = "2026-11-10"   # before both expiries
        chain = [
            mk_quote(strike=95.0, delta=-0.15, bid=0.90),
            mk_quote(cp="C", strike=111.0, delta=0.10, bid=0.85),
        ]
        rows, _ = self._gen(chain, bundle=bundle)
        self.assertFalse([r for r in rows if r["short_expiry"] == "2026-11-13"])

    def test_adverse_sweep_vetoes_strike(self):
        trades = [{
            "option_type": "put", "strike": 95.0, "ask_vol": 500, "bid_vol": 10,
            "size": 5000, "open_interest": 100, "premium": 250_000,
        }]
        chain = [mk_quote(strike=95.0, delta=-0.15, bid=0.90)]
        rows, _ = self._gen(chain, trades=trades)
        self.assertFalse([r for r in rows if r["strategy"] == "SHORT_PUT"])

    def test_weak_sector_blocks_bull_side(self):
        health = {"Semiconductor": {"mean_rsi": 35.0, "mean_flow": 0.0, "members": 5}}
        chain = [mk_quote(strike=95.0, delta=-0.15, bid=0.90)]
        rows, vetoes = self._gen(chain, sector_health=health)
        self.assertFalse([r for r in rows if r["strategy"] in BULL_SIDE_STRATEGIES])
        self.assertTrue(any("SECTOR_WEAK_RSI" in v for v in vetoes))

    def test_single_legs_blocked_above_600(self):
        # rule 2026-11-08: no naked short put/call on tickers above $600
        bundle = mk_bundle(spot=700.0, verdict="bearish", rsi=65.0, dp_accumulating=False)
        chain = [
            mk_quote(strike=650.0, delta=-0.15, bid=3.00),                 # put single -> blocked
            mk_quote(cp="C", strike=730.0, delta=0.12, bid=4.00),          # call single -> blocked
            mk_quote(cp="C", strike=710.0, delta=0.18, bid=6.00),          # BCS short
            mk_quote(cp="C", strike=715.0, delta=0.10, bid=4.00, ask=4.80),  # BCS long (credit 1.20)
        ]
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=650.0, call_wall=710.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=650.0, call_wall=710.0, source="bucket_chain"),
        }
        rows, vetoes = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_OFF",
        )
        self.assertFalse([r for r in rows if r["strategy"] in ("SHORT_PUT", "SHORT_CALL")])
        self.assertTrue(any("SPOT_ABOVE_600" in v for v in vetoes))
        # spreads on the same >$600 ticker remain allowed
        self.assertTrue([r for r in rows if r["strategy"] == "BEAR_CALL_SPREAD"])

    def test_single_legs_allowed_below_600(self):
        bundle = mk_bundle(spot=550.0)
        chain = [mk_quote(strike=520.0, delta=-0.15, bid=2.50)]   # ann ~29% clears the 25% LT8 gate
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=520.0, call_wall=580.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=520.0, call_wall=580.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        self.assertTrue([r for r in rows if r["strategy"] == "SHORT_PUT"])

    def test_nontech_sector_prefers_ten_wide(self):
        # 2026-11-08 rule: non-tech sectors trade $10-wide verticals (tech keeps $5)
        bundle = mk_bundle(sector="Consumer Staples")
        chain = [
            mk_quote(strike=95.0, delta=-0.18, bid=2.00),               # 5-wide leg exists
            mk_quote(strike=90.0, delta=-0.10, bid=1.40, ask=1.50),
            mk_quote(strike=85.0, delta=-0.06, bid=0.70, ask=0.80),
        ]
        # 5-wide credit 0.50 fails the $0.70 gate; 10-wide credit 1.20, RoR 13.6% passes (floor 0.10)
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertTrue(bps)
        self.assertEqual(bps[0]["spread_width"], 10.0)
        self.assertEqual(bps[0]["leg2_strike"], 85.0)

    def test_tech_sector_still_prefers_five_wide(self):
        bundle = mk_bundle(sector="Semiconductor")
        chain = [
            mk_quote(strike=95.0, delta=-0.18, bid=2.00),
            mk_quote(strike=90.0, delta=-0.10, bid=1.10, ask=1.20),   # 5-wide credit 0.80, RoR 19% -> OK
            mk_quote(strike=85.0, delta=-0.06, bid=0.70, ask=0.80),   # 10-wide credit 1.20 also OK
        ]
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        bps = [r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"]
        self.assertTrue(bps)
        self.assertEqual(bps[0]["spread_width"], 5.0)
        self.assertEqual(bps[0]["leg2_strike"], 90.0)

    def test_ror_floor_scaled_by_width(self):
        # 10-wide RoR 8.7% (below the 0.10 wide-spread floor) must fail even though credit > $0.70
        bundle = mk_bundle(sector="Consumer Staples")
        chain = [
            mk_quote(strike=95.0, delta=-0.16, bid=1.10),             # 10-wide credit 0.80 -> RoR 8.7%
            mk_quote(strike=85.0, delta=-0.08, bid=0.20, ask=0.30),
        ]
        walls = {
            "LT8": BucketWalls(bucket="LT8", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
            "DTE8_14": BucketWalls(bucket="DTE8_14", put_wall=95.0, call_wall=110.0, source="bucket_chain"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, option_trades=None, sector_health={}, market_regime="RISK_ON",
        )
        self.assertFalse([r for r in rows if r["strategy"] == "BULL_PUT_SPREAD"])

    def test_unknown_sector_never_blocks(self):
        health = {"Unknown": {"mean_rsi": 20.0, "mean_flow": -80.0, "members": 3}}
        bundle = mk_bundle(sector="Unknown")
        chain = [mk_quote(strike=95.0, delta=-0.15, bid=0.90)]
        rows, _ = self._gen(chain, bundle=bundle, sector_health=health)
        self.assertTrue([r for r in rows if r["strategy"] == "SHORT_PUT"])

    def test_dte_bucket_boundaries(self):
        # DTE 1 and 7 in LT8; 8 and 14 in DTE8_14; 15 excluded
        chain = [
            mk_quote(expiry="2026-11-09", dte=2, strike=95.0, delta=-0.12, bid=0.60),
            mk_quote(expiry="2026-11-13", dte=6, strike=95.0, delta=-0.15, bid=0.90),
            mk_quote(expiry="2026-11-20", dte=13, strike=95.0, delta=-0.15, bid=1.10),
            mk_quote(expiry="2026-11-27", dte=20, strike=95.0, delta=-0.15, bid=1.30),
        ]
        bundle = mk_bundle()
        walls = {
            "LT8": mk_walls("LT8"),
            "DTE8_14": mk_walls("DTE8_14"),
        }
        rows, _ = generate_opportunities_for_symbol(
            REPORT_DATE, "run-1", bundle, chain, walls, market_regime="RISK_ON"
        )
        sp_dtes = {r["short_dte"] for r in rows if r["strategy"] == "SHORT_PUT"}
        self.assertTrue(sp_dtes.issubset({2, 6, 13}))
        self.assertNotIn(20, sp_dtes)
        buckets = {r["dte_bucket"] for r in rows if r["strategy"] == "SHORT_PUT"}
        self.assertIn("LT8", buckets)
        self.assertIn("DTE8_14", buckets)


# ---------------------------------------------------------------------------
# Quota planner
# ---------------------------------------------------------------------------


def mk_candidate(i, symbol, strategy, bucket="LT8", sector="Semiconductor", score=70.0, credit=2.0,
                 collateral=None, sector_rsi=None, sector_flow=None):
    return {
        "opportunity_key": f"k{i}",
        "symbol": symbol,
        "strategy": strategy,
        "dte_bucket": bucket,
        "sector": sector,
        "entry_credit": credit,
        "confidence_score": score,
        "market_regime": "UNKNOWN",
        "collateral": collateral if collateral is not None else (5.0 if strategy in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD") else 50.0),
        "sector_health_rsi": sector_rsi,
        "sector_health_flow": sector_flow,
        "planned_contracts": 0,
        "plan_rank": None,
        "plan_score_adjusted": None,
        "plan_week": None,
    }


class TestQuotaPlanner(unittest.TestCase):
    def test_defaults_forty_contracts_twenty_tickers(self):
        # 2026-11-08 rule change: defaults are now 40 contracts over 20 tickers
        cands = []
        i = 0
        for t in range(24):
            sym = f"T{t:02d}"
            for strat in ("SHORT_PUT", "SHORT_CALL", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
                for bucket in ("LT8", "DTE8_14"):
                    cands.append(mk_candidate(i, sym, strat, bucket, sector=f"Sec{t % 12}", score=60.0 + i * 0.1))
                    i += 1
        plan = plan_weekly_quota(cands, REPORT_DATE)
        s = plan["summary"]
        self.assertEqual(s["target_contracts"], 40)
        self.assertEqual(s["planned_contracts"], 40)
        self.assertEqual(s["planned_tickers"], 20)
        self.assertEqual(s["single_leg_planned"], 16)      # 40% of 40
        self.assertEqual(s["spread_target"], 24)           # 60% of 40
        per_symbol = {}
        for c in plan["picked"]:
            per_symbol[c["symbol"]] = per_symbol.get(c["symbol"], 0) + c["planned_contracts"]
        self.assertTrue(all(v <= 5 for v in per_symbol.values()))

    def test_ten_tickers_forty_sixty(self):
        cands = []
        i = 0
        for t in range(12):
            sym = f"S{t:02d}"
            for strat in ("SHORT_PUT", "SHORT_CALL", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
                for bucket in ("LT8", "DTE8_14"):
                    cands.append(mk_candidate(i, sym, strat, bucket, sector=f"Sec{t % 6}", score=60.0 + i * 0.1))
                    i += 1
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        self.assertEqual(s["planned_contracts"], 20)
        self.assertEqual(s["planned_tickers"], 10)
        self.assertEqual(s["single_leg_planned"], 8)     # 40%
        self.assertGreaterEqual(s["spread_planned"], 12)  # 60% (may grow via spill)
        per_symbol = {}
        for c in plan["picked"]:
            per_symbol[c["symbol"]] = per_symbol.get(c["symbol"], 0) + c["planned_contracts"]
        self.assertTrue(all(v <= 5 for v in per_symbol.values()))

    def test_spread_orders_sized_up_to_five(self):
        # few spread rows only: Phase B must size them up to the 5-contract order cap
        cands = []
        i = 0
        for sym in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"):
            cands.append(mk_candidate(i, sym, "BULL_PUT_SPREAD", sector=f"S{i}", score=80.0, credit=1.5))
            i += 1
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        self.assertEqual(s["planned_contracts"], 20)
        self.assertTrue(all((c["planned_contracts"] or 0) <= 5 for c in plan["picked"]))
        # 12 spread rows, 20 contracts -> round-robin gives 2,2,...,2,1,... (max < 5 here)
        self.assertEqual(s["spread_planned"], 20 - s["single_leg_planned"])

    def test_spread_order_cap_five_when_pool_small(self):
        # only 3 spread rows -> sizing must cap each at 5 (15 spread contracts max from Phase B)
        cands = [mk_candidate(1, "A", "BEAR_CALL_SPREAD", sector="S1", score=90.0, credit=2.0),
                 mk_candidate(2, "B", "BEAR_CALL_SPREAD", sector="S2", score=85.0, credit=2.0),
                 mk_candidate(3, "C", "BEAR_CALL_SPREAD", sector="S3", score=80.0, credit=2.0)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["planned_contracts"], 3 + 12)  # 3 rows + top-up to 5 each
        self.assertTrue(all(c["planned_contracts"] <= 5 for c in plan["picked"]))

    def test_ticker_total_capped_across_two_spread_rows(self):
        # one symbol with two spread rows (both buckets) must not exceed 5 contracts TOTAL
        cands = [mk_candidate(1, "A", "BEAR_CALL_SPREAD", bucket="LT8", sector="S1", score=90.0, credit=2.0),
                 mk_candidate(2, "A", "BEAR_CALL_SPREAD", bucket="DTE8_14", sector="S1", score=88.0, credit=2.0)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        total_a = sum(c["planned_contracts"] for c in plan["picked"] if c["symbol"] == "A")
        self.assertEqual(total_a, 5)

    def test_collateral_cap_blocks_expensive_singles(self):
        # cash-secured puts at strike 900 = $90,000 collateral each; cap $100k
        cands = [mk_candidate(i, f"P{i}", "SHORT_PUT", sector=f"S{i}", score=90.0, credit=5.0, collateral=900.0)
                 for i in range(4)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        self.assertEqual(s["planned_contracts"], 1)
        self.assertLessEqual(s["weekly_collateral_usd"], 100_000.0)

    def test_collateral_reported_in_summary(self):
        cands = [mk_candidate(1, "A", "SHORT_PUT", sector="S", score=90.0, credit=3.0, collateral=100.0),
                 mk_candidate(2, "B", "BULL_PUT_SPREAD", sector="T", score=80.0, credit=1.0, collateral=5.0)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        # 1 short put ($10,000) + spread row sized up... spread pool 12, single pool 8:
        # only 1 spread row -> sized to 5 contracts ($2,500)
        self.assertEqual(s["weekly_collateral_usd"], 10_000.0 + 5 * 500.0)
        self.assertGreaterEqual(s["collateral_headroom_usd"], 0)

    def test_short_call_cap_four(self):
        cands = [mk_candidate(i, f"SC{i}", "SHORT_CALL", sector=f"S{i}", score=90.0, credit=3.0) for i in range(10)]
        cands += [mk_candidate(100 + i, f"SP{i}", "SHORT_PUT", sector=f"P{i}", score=50.0, credit=1.0) for i in range(10)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["short_call_planned"], 4)
        self.assertEqual(plan["summary"]["single_leg_planned"], 8)

    def test_sector_cap_two(self):
        cands = [mk_candidate(i, f"S{i}", "SHORT_PUT", sector="Semiconductor", score=70.0 + i) for i in range(8)]
        cands += [mk_candidate(50 + i, f"O{i}", "SHORT_PUT", sector="Energy", score=40.0 + i) for i in range(8)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        sectors = {}
        for c in plan["picked"]:
            sectors[c["sector"]] = sectors.get(c["sector"], 0) + 1
        # max 2 tickers per sector -> at most 4 contracts (2 each) from semis
        self.assertLessEqual(sectors.get("Semiconductor", 0), 4)

    def test_trend_aligned_picks_exempt_from_sector_cap(self):
        # bear sector (mean RSI 38) + bear-side strategies: ALL qualified tickers may enter
        cands = [mk_candidate(i, f"B{i}", "BEAR_CALL_SPREAD", sector="Semiconductor",
                              score=70.0 + i, sector_rsi=38.0) for i in range(8)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        semi_tickers = {c["symbol"] for c in plan["picked"] if c["sector"] == "Semiconductor"}
        self.assertEqual(len(semi_tickers), 8)       # not capped at 2

    def test_contrary_picks_still_capped(self):
        # bull-side strategies in a bear-trend sector stay capped at 2 tickers
        cands = [mk_candidate(i, f"S{i}", "BULL_PUT_SPREAD", sector="Semiconductor",
                              score=70.0 + i, sector_rsi=40.0) for i in range(8)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        semi_tickers = {c["symbol"] for c in plan["picked"] if c["sector"] == "Semiconductor"}
        self.assertLessEqual(len(semi_tickers), 2)

    def test_unknown_sector_exempt_from_cap(self):
        cands = [mk_candidate(i, f"U{i}", "SHORT_PUT", sector="Unknown", score=70.0 + i) for i in range(10)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["planned_contracts"], 8)  # single-leg pool only

    def test_both_buckets_used_when_available(self):
        cands = [mk_candidate(i, f"A{i // 2}", "BULL_PUT_SPREAD", "DTE8_14", score=80.0) for i in range(10)]
        cands += [mk_candidate(100 + i, f"B{i // 2}", "BULL_PUT_SPREAD", "LT8", score=50.0) for i in range(10)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        buckets = plan["summary"]["bucket_split"]
        self.assertGreater(buckets.get("LT8", 0), 0)
        self.assertGreater(buckets.get("DTE8_14", 0), 0)

    def test_projected_credit_reported(self):
        cands = [
            mk_candidate(1, "A", "SHORT_PUT", score=90.0, credit=3.0),
            mk_candidate(2, "B", "SHORT_CALL", score=85.0, credit=2.5),
        ]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["projected_weekly_credit_usd"], 550.0)
        self.assertEqual(plan["summary"]["credit_target_usd"], 5000.0)
        self.assertEqual(plan["summary"]["avg_credit_per_contract_usd"], 275.0)

    def test_nontech_ticker_reserve(self):
        # 2026-11-08 rule: >=5 planned tickers must come from non-tech sectors when
        # qualified non-tech candidates exist, even when tech candidates score higher.
        # The reserve operates WITHIN the <=2-tickers/sector cap, so it needs several
        # non-tech sectors to be satisfiable.
        cands = []
        i = 0
        for t in range(12):
            cands.append(mk_candidate(i, f"TECH{i}", "BEAR_CALL_SPREAD", sector="Semiconductor", score=95.0, credit=2.0))
            i += 1
        for sec in ("Consumer Staples", "Healthcare", "Energy"):
            for t in range(4):
                cands.append(mk_candidate(100 + i, f"{sec[:3]}{t}", "BEAR_CALL_SPREAD", sector=sec, score=60.0, credit=1.0))
                i += 1
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        nontech_syms = {c["symbol"] for c in plan["picked"] if is_nontech_row(c)}
        self.assertGreaterEqual(len(nontech_syms), 5)
        self.assertTrue(s["nontech_reserve_met"])
        self.assertEqual(s["nontech_tickers_planned"], len(nontech_syms))
        # sector cap still respected within the reserve
        from collections import Counter
        by_sec = Counter(c["sector"] for c in plan["picked"] if c["symbol"] in nontech_syms)
        self.assertTrue(all(v <= 2 for v in by_sec.values()))

    def test_nontech_reserve_shortfall_reported(self):
        # only 2 non-tech candidates qualify -> reserve unmet but fill continues
        cands = [mk_candidate(i, f"T{i}", "BEAR_CALL_SPREAD", sector="Semiconductor", score=90.0) for i in range(12)]
        cands += [mk_candidate(100, "N1", "BEAR_CALL_SPREAD", sector="Consumer Staples", score=60.0),
                  mk_candidate(101, "N2", "BEAR_CALL_SPREAD", sector="Healthcare", score=60.0)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        s = plan["summary"]
        self.assertFalse(s["nontech_reserve_met"])
        self.assertEqual(s["nontech_tickers_planned"], 2)
        self.assertEqual(s["planned_contracts"], 20)   # fill not blocked by the reserve

    def test_unknown_sector_not_counted_for_reserve(self):
        cands = [mk_candidate(i, f"U{i}", "BEAR_CALL_SPREAD", sector="Unknown", score=90.0) for i in range(12)]
        plan = plan_weekly_quota(cands, REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["nontech_tickers_planned"], 0)

    def test_shortfall_reported_honestly(self):
        plan = plan_weekly_quota([mk_candidate(1, "A", "SHORT_PUT", score=90.0)], REPORT_DATE, weekly_contracts=20, max_tickers=10)
        self.assertEqual(plan["summary"]["planned_contracts"], 1)
        self.assertEqual(plan["summary"]["planned_tickers"], 1)


# ---------------------------------------------------------------------------
# Regime, sector health, flow
# ---------------------------------------------------------------------------


class TestRegimeAndSector(unittest.TestCase):
    def test_regime_classification(self):
        self.assertEqual(classify_market_regime("Risk-On (inferred: SPY/QQQ orderly uptrends)"), "RISK_ON")
        self.assertEqual(classify_market_regime("Risk-Off: distribution broadening"), "RISK_OFF")
        self.assertEqual(classify_market_regime("Choppy range, no trend"), "CHOP")
        self.assertEqual(classify_market_regime(None), "UNKNOWN")

    def test_regime_tilt_directions(self):
        self.assertGreater(regime_tilt("SHORT_PUT", "RISK_ON"), 0)
        self.assertLess(regime_tilt("BEAR_CALL_SPREAD", "RISK_ON"), 0)
        self.assertGreater(regime_tilt("BEAR_CALL_SPREAD", "RISK_OFF"), 0)
        self.assertLess(regime_tilt("SHORT_PUT", "RISK_OFF"), 0)
        self.assertGreater(regime_tilt("BULL_PUT_SPREAD", "CHOP"), 0)
        self.assertLess(regime_tilt("SHORT_CALL", "CHOP"), 0)

    def test_sector_health_aggregation(self):
        b1 = mk_bundle(symbol="A", sector="Energy", rsi=30.0)
        b2 = mk_bundle(symbol="B", sector="Energy", rsi=50.0)
        b1.flow_composite, b2.flow_composite = -40.0, -20.0
        h = aggregate_sector_health([b1, b2])
        self.assertEqual(h["Energy"]["members"], 2)
        self.assertEqual(h["Energy"]["mean_rsi"], 40.0)
        self.assertEqual(h["Energy"]["mean_flow"], -30.0)
        self.assertIsNotNone(sector_blocks_side("Energy", "bull", h))

    def test_flow_labels(self):
        self.assertEqual(flow_label_for(45), "strong_bull")
        self.assertEqual(flow_label_for(20), "bull")
        self.assertEqual(flow_label_for(0), "neutral")
        self.assertEqual(flow_label_for(-20), "bear")
        self.assertEqual(flow_label_for(-45), "strong_bear")


# ---------------------------------------------------------------------------
# Earnings clearance & lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle(unittest.TestCase):
    def test_earnings_clear_rules(self):
        self.assertFalse(is_earnings_clear(date(2026, 11, 7), date(2026, 11, 13), "2026-11-10"))
        self.assertTrue(is_earnings_clear(date(2026, 11, 7), date(2026, 11, 13), "2026-11-21"))
        self.assertTrue(is_earnings_clear(date(2026, 11, 7), date(2026, 11, 13), "2026-11-13", "postmarket"))
        self.assertFalse(is_earnings_clear(date(2026, 11, 7), date(2026, 11, 13), "2026-11-13", "premarket"))

    def _row(self, strategy="SHORT_PUT", credit=1.00, strike=95.0, dte=6, leg2=None, width=None):
        return {
            "opportunity_key": "k",
            "strategy": strategy,
            "short_expiry": "2026-11-13",
            "short_dte": dte,
            "short_strike": strike,
            "leg2_strike": leg2,
            "spread_width": width,
            "entry_credit": credit,
            "status": "PROPOSED",
            "outcome": None,
            "win": None,
        }

    def test_fifty_percent_profit_auto_win(self):
        row = self._row(credit=1.00)
        q = mk_quote(strike=95.0, bid=0.40, ask=0.48)  # close ask 0.48 <= 0.50
        out = evaluate_open_opportunity_row(row, date(2026, 11, 10), 100.0, short_quote=q)
        self.assertEqual(out["outcome"], "WON")
        self.assertTrue(out["win"])
        self.assertEqual(out["status"], "CLOSED")

    def test_wall_breach_defense(self):
        row = self._row()
        q = mk_quote(strike=95.0, bid=0.90, ask=1.00)
        out = evaluate_open_opportunity_row(
            row, date(2026, 11, 10), 99.0, short_quote=q,
            current_bucket_put_wall=90.0,  # wall fell below the short strike
        )
        self.assertEqual(out["defense_reason"], "BUCKET_PUT_WALL_BREACH")

    def test_expiry_settlement_worthless_and_lost(self):
        q = None
        win = evaluate_open_opportunity_row(self._row(), date(2026, 11, 13), 100.0, short_quote=q)
        self.assertEqual(win["outcome"], "EXPIRED_WORTHLESS")
        self.assertTrue(win["win"])
        lost = evaluate_open_opportunity_row(self._row(), date(2026, 11, 13), 90.0, short_quote=q)
        self.assertEqual(lost["outcome"], "LOST")
        self.assertFalse(lost["win"])

    def test_bps_expiry_settlement_capped(self):
        row = self._row(strategy="BULL_PUT_SPREAD", credit=1.00, strike=95.0, leg2=90.0, width=5.0)
        out = evaluate_open_opportunity_row(row, date(2026, 11, 13), 85.0, short_quote=None)
        # spot 85 << long 90: intrinsic 5.00, pnl = 1.00 - 5.00 = -4.00 (loss capped at width)
        self.assertEqual(out["exit_pnl"], -4.0)
        self.assertFalse(out["win"])

    def test_midcycle_mark_recorded_once(self):
        row = self._row(dte=6)
        q = mk_quote(strike=95.0, bid=0.60, ask=0.70)
        out = evaluate_open_opportunity_row(row, date(2026, 11, 10), 100.0, short_quote=q)
        self.assertIsNotNone(out["midcycle_mark_date"])
        out2 = evaluate_open_opportunity_row(out, date(2026, 11, 11), 100.0, short_quote=q)
        self.assertEqual(out2["midcycle_mark_date"], out["midcycle_mark_date"])


# ---------------------------------------------------------------------------
# Universe builder
# ---------------------------------------------------------------------------


class TestUniverse(unittest.TestCase):
    def test_categories_and_dedup(self):
        rows = [
            {"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
            {"symbol": "NVDA", "category": "watch1", "exchange": "NASDAQ"},   # dedup keeps core
            {"symbol": "BESIY", "category": "watch1", "exchange": "OTC"},     # dropped
            {"symbol": "IQE.L", "category": "watch1", "exchange": "LSE"},     # dropped
            {"symbol": "PLTR", "category": "watch1", "exchange": "NYSE"},
        ]
        uni = build_watchlist_universe(rows)
        self.assertEqual(uni, [("NVDA", "core"), ("PLTR", "watch1")])

    def test_override_only_narrows(self):
        rows = [{"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
                {"symbol": "PLTR", "category": "watch1", "exchange": "NYSE"}]
        uni = build_watchlist_universe(rows, override=["PLTR", "TSLA"])
        self.assertEqual(uni, [("PLTR", "watch1")])

    def test_all_categories_includes_watch2_and_watch3(self):
        rows = [
            {"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
            {"symbol": "JPM", "category": "watch2", "exchange": "NYSE"},
            {"symbol": "WMT", "category": "watch3", "exchange": "NYSE"},
            {"symbol": "AIR", "category": "euro", "exchange": "XETR"},          # non-US, dropped
        ]
        uni = build_watchlist_universe(rows, categories=("core", "satellite", "watch1", "watch2", "watch3", "euro"))
        self.assertEqual([s for s, _ in uni], ["NVDA", "JPM", "WMT"])

    def test_default_excludes_watch2(self):
        rows = [
            {"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
            {"symbol": "JPM", "category": "watch2", "exchange": "NYSE"},
        ]
        uni = build_watchlist_universe(rows)
        self.assertEqual([s for s, _ in uni], ["NVDA"])


class TestPlanWeekKey(unittest.TestCase):
    def test_iso_week(self):
        self.assertEqual(iso_week_key(date(2026, 11, 7)), "2026-W45")


if __name__ == "__main__":
    unittest.main(verbosity=2)
