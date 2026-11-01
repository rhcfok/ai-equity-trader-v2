"""
Offline unit tests for uw_options_income_runner.py (no network, no credentials).

Run:
    python3 skills/uw-options-income-engine/tests/test_uw_options_income_runner.py
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re
import sys
import unittest

try:
    import pytest  # type: ignore
except ImportError:  # pragma: no cover - stdlib fallback when pytest is not installed
    class _Approx:
        def __init__(self, expected: float, abs: float = 1e-4):
            self.expected = expected
            self.abs = abs

        def __eq__(self, other: object) -> bool:
            return isinstance(other, (int, float)) and abs(float(other) - self.expected) <= self.abs

        def __repr__(self) -> str:
            return f"{self.expected} ± {self.abs}"

    class _PytestStub:
        @staticmethod
        def approx(expected: float, abs: float = 1e-4) -> _Approx:
            return _Approx(expected, abs)

    pytest = _PytestStub()  # type: ignore

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import uw_options_income_runner as r  # noqa: E402

TODAY = date(2026, 10, 19)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def occ(sym: str, exp: date, cp: str, strike: float) -> str:
    return f"{sym}{exp.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


def q(sym, exp, cp, strike, bid, ask, delta, oi=2000, iv=0.40):
    return r.OptionContractQuote(
        option_symbol=occ(sym, exp, cp, strike),
        underlying_symbol=sym,
        option_type="call" if cp == "C" else "put",
        expiry=exp.isoformat(),
        dte=(exp - TODAY).days,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        iv=iv,
        open_interest=oi,
    )


def earnings_rows(eps_list, surprise=5.0, next_report=None):
    """eps_list newest-first quarterly EPS."""
    rows = []
    for i, eps in enumerate(eps_list):
        fq = TODAY - timedelta(days=45 + 91 * i)
        rows.append(
            {
                "report_type": "quarterly",
                "fiscal_date_ending": fq.isoformat(),
                "report_date": (fq + timedelta(days=25)).isoformat(),
                "reported_eps": str(eps),
                "estimated_eps": str(eps * 0.95),
                "surprise_percentage": str(surprise) if i == 0 else "1.0",
            }
        )
    if next_report:
        rows.append(
            {
                "report_type": "quarterly",
                "report_date": next_report.isoformat(),
                "report_time": "postmarket",
                "reported_eps": None,
            }
        )
    return rows


def base_bundle(**overrides) -> r.SymbolSignalBundle:
    val = r.compute_uw_valuation_context(
        "ABC", 100.0, earnings_rows([1.5, 1.4, 1.3, 1.2, 1.1, 1.0, 0.95, 0.9]), [], as_of_date=TODAY
    )
    vol = r.UWVolatilityContext(
        symbol="ABC", iv7=0.40, iv30=0.40, iv60=0.40, iv_rank=40.0, iv_percentile=40.0,
        term_ratio_7_30=1.0, term_ratio_30_60=1.0, rsi_14=48.0,
    )
    b = r.SymbolSignalBundle(
        symbol="ABC",
        watchlist_category="core",
        spot_price=100.0,
        verdict="bullish",
        jlaw_classification="Valid",
        jlaw_score=12,
        jlaw_stop_loss=94.0,
        jlaw_target_price=112.0,
        gex_regime="positive",
        gex_put_wall=95.0,
        gex_call_wall=108.0,
        gex_net_mm=50.0,
        dp_top_level=96.0,
        dp_accumulating=True,
        valuation=val,
        volatility=vol,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


EXP_32 = TODAY + timedelta(days=32)
EXP_7 = TODAY + timedelta(days=7)
EXP_21 = TODAY + timedelta(days=21)
EXP_35 = TODAY + timedelta(days=35)
EXP_75 = TODAY + timedelta(days=75)


# ---------------------------------------------------------------------------
# Helpers & parsing
# ---------------------------------------------------------------------------


def test_parse_occ_symbol():
    p = r.parse_occ_symbol("NVDA261120P00150000")
    assert p == {"underlying_symbol": "NVDA", "expiry": "2026-11-20", "option_type": "put", "strike": 150.0}
    assert r.parse_occ_symbol("garbage") is None


def test_us_symbol_filter():
    assert r.is_us_option_symbol("NVDA")
    assert not r.is_us_option_symbol("0700.HK")
    assert not r.is_us_option_symbol("2330")


# ---------------------------------------------------------------------------
# UW valuation replacement for equity_valuation_signal_daily
# ---------------------------------------------------------------------------


def test_valuation_ttm_pe_growth_peg():
    v = r.compute_uw_valuation_context(
        "ABC", 100.0, earnings_rows([1.5, 1.4, 1.3, 1.2, 1.1, 1.0, 0.95, 0.9]), [], as_of_date=TODAY
    )
    assert v.ttm_eps == pytest.approx(5.4)
    assert v.pe_ratio == pytest.approx(18.52, abs=0.01)
    assert v.eps_growth_yoy_pct == pytest.approx((5.4 - 3.95) / 3.95 * 100, abs=0.01)
    assert v.peg_ratio is not None and v.peg_ratio < 1.2
    assert v.valuation_tier == "cheap"
    assert v.wheel_fundamental_ok is True


def test_valuation_negative_eps_blocks_wheel():
    v = r.compute_uw_valuation_context("ABC", 50.0, earnings_rows([-0.2, -0.1, 0.05, -0.3]), [], as_of_date=TODAY)
    assert v.valuation_tier == "not_meaningful"
    assert v.wheel_fundamental_ok is False


def test_valuation_expensive_tier():
    v = r.compute_uw_valuation_context("ABC", 500.0, earnings_rows([1.0, 1.0, 1.0, 1.0]), [], as_of_date=TODAY)
    assert v.pe_ratio == pytest.approx(125.0)
    assert v.valuation_tier == "expensive"
    assert v.wheel_fundamental_ok is False


def test_valuation_upcoming_earnings_detected():
    nxt = TODAY + timedelta(days=10)
    v = r.compute_uw_valuation_context("ABC", 100.0, earnings_rows([1, 1, 1, 1], next_report=nxt), [], as_of_date=TODAY)
    assert v.earnings_date == nxt.isoformat()
    assert v.earnings_report_time == "postmarket"


def test_valuation_recent_downgrade_flag_when_analyst_rows_supplied():
    rows = [{"timestamp": (TODAY - timedelta(days=3)).isoformat(), "action": "downgraded", "target": "90"}]
    v = r.compute_uw_valuation_context("ABC", 100.0, earnings_rows([1.5, 1.4, 1.3, 1.2]), rows, as_of_date=TODAY)
    assert v.recent_downgrade_14d is True
    assert v.wheel_fundamental_ok is False


# ---------------------------------------------------------------------------
# Volatility context
# ---------------------------------------------------------------------------


def test_volatility_context_term_ratios_and_rsi():
    iv_rows = [
        {"days": 7, "volatility": "0.50", "percentile": "0.62"},
        {"days": 30, "volatility": "0.40", "percentile": "0.55"},
        {"days": 60, "volatility": "0.38", "percentile": "0.50"},
        {"days": 90, "volatility": "0.37", "percentile": "0.48"},
    ]
    rsi_rows = [
        {"date": "2026-10-16", "values": {"RSI": "47.20"}},
        {"date": "2026-10-15", "values": {"RSI": "44.00"}},
    ]
    v = r.compute_uw_volatility_context("ABC", iv_rows, rsi_rows=rsi_rows)
    assert v.iv_rank == pytest.approx(55.0)
    assert v.term_ratio_7_30 == pytest.approx(1.25)
    assert v.term_ratio_30_60 == pytest.approx(round(0.40 / 0.38, 3))
    assert v.rsi_14 == pytest.approx(47.2)


# ---------------------------------------------------------------------------
# Strike-level sweep veto
# ---------------------------------------------------------------------------


def test_adverse_put_sweep_detected_near_strike():
    trades = [
        {"option_type": "put", "strike": "95", "ask_vol": 900, "bid_vol": 10, "size": 900, "open_interest": 300, "premium": 80000},
        {"option_type": "put", "strike": "80", "ask_vol": 900, "bid_vol": 10, "size": 900, "open_interest": 300},
        {"option_type": "put", "strike": "95", "ask_vol": 5, "bid_vol": 500, "size": 500, "open_interest": 3000, "premium": 60000},
    ]
    adv, bids = r.evaluate_strike_flow_prints(trades, "put", 95.0)
    assert adv == 1
    assert bids == 1


# ---------------------------------------------------------------------------
# Floors, ceilings, earnings clearance
# ---------------------------------------------------------------------------


def test_safety_floor_is_min_of_valid_anchors():
    b = base_bundle()
    assert r.compute_safety_floor(b) == 94.0
    b2 = base_bundle(gex_regime="sub_resolution", jlaw_stop_loss=None, dp_accumulating=False)
    assert r.compute_safety_floor(b2) is None


def test_safety_ceiling():
    assert r.compute_safety_ceiling(base_bundle()) == 112.0
    b = base_bundle(gex_call_wall=None, jlaw_target_price=None)
    assert r.compute_safety_ceiling(b) == pytest.approx(104.0)


def test_earnings_clear_rules():
    exp = TODAY + timedelta(days=30)
    assert r.is_earnings_clear(TODAY, exp, None)
    assert not r.is_earnings_clear(TODAY, exp, (TODAY + timedelta(days=10)).isoformat())
    assert r.is_earnings_clear(TODAY, exp, (TODAY + timedelta(days=40)).isoformat())
    assert r.is_earnings_clear(TODAY, exp, exp.isoformat(), "postmarket")
    assert not r.is_earnings_clear(TODAY, exp, exp.isoformat(), "premarket")


# ---------------------------------------------------------------------------
# Strategy builders
# ---------------------------------------------------------------------------


def test_wheel_csp_core_selected_below_floor():
    b = base_bundle()
    chain = [
        q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24),   # below floor 94, qualifies
        q("ABC", EXP_32, "P", 97.0, 2.80, 2.90, -0.32),   # above floor & delta too high
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    csp = [x for x in rows if x["strategy"] == "WHEEL_CSP" and x["dte_bucket"] == "CORE"]
    assert len(csp) == 1
    assert csp[0]["short_strike"] == 93.0
    assert csp[0]["annualized_pct"] >= 20.0
    assert csp[0]["uw_valuation_tier"] == "cheap"


def test_wheel_csp_vetoed_by_adverse_sweep():
    b = base_bundle()
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    trades = [{"option_type": "put", "strike": 93, "ask_vol": 800, "bid_vol": 0, "size": 800, "open_interest": 100}]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, trades)
    assert not [x for x in rows if x["strategy"] == "WHEEL_CSP"]


def test_wheel_csp_blocked_when_fundamentals_fail():
    b = base_bundle()
    b.valuation = r.compute_uw_valuation_context("ABC", 100.0, earnings_rows([-1, -1, -1, -1]), [], as_of_date=TODAY)
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    assert not [x for x in rows if x["strategy"] == "WHEEL_CSP"]


def test_wheel_csp_blocked_by_earnings_inside_expiry():
    b = base_bundle()
    b.valuation.earnings_date = (TODAY + timedelta(days=12)).isoformat()
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    assert not [x for x in rows if x["strategy"] == "WHEEL_CSP"]


def test_wheel_csp_tactical_bucket():
    b = base_bundle()
    chain = [q("ABC", EXP_7, "P", 93.0, 0.45, 0.50, -0.12)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    tac = [x for x in rows if x["strategy"] == "WHEEL_CSP" and x["dte_bucket"] == "TACTICAL"]
    assert len(tac) == 1


def test_wheel_csp_routes_allowed_watch2_category():
    b = base_bundle(watchlist_category=" Watch2 ")
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    csp = [x for x in rows if x["strategy"] == "WHEEL_CSP"]
    assert len(csp) == 1
    assert b.watchlist_category == " Watch2 "
    assert csp[0]["watchlist_category"] == "watch2"


def test_bull_put_spread_adaptive_width_and_delta_cap():
    b = base_bundle()
    chain = [
        q("ABC", EXP_21, "P", 93.0, 1.60, 1.70, -0.25),
        q("ABC", EXP_21, "P", 88.0, 0.55, 0.60, -0.10),
        q("ABC", EXP_21, "P", 90.0, 1.30, 1.40, -0.31),  # delta > 0.28 — must never be short leg
        q("ABC", EXP_21, "P", 85.0, 0.30, 0.35, -0.07),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    bps = [x for x in rows if x["strategy"] == "BULL_PUT_SPREAD"]
    assert len(bps) == 1
    assert bps[0]["spread_width"] == 5.0  # spot < 150
    assert abs(bps[0]["short_delta"]) <= 0.28
    assert 0.20 <= bps[0]["return_on_risk"] <= 0.35
    assert bps[0]["entry_credit"] == pytest.approx(1.60 - 0.60)


def test_bull_put_spread_ten_wide_for_high_price():
    b = base_bundle(spot_price=200.0, jlaw_stop_loss=188.0, gex_put_wall=190.0, gex_call_wall=215.0,
                    dp_top_level=192.0, jlaw_target_price=225.0)
    b.valuation = r.compute_uw_valuation_context("ABC", 200.0, earnings_rows([3, 3, 3, 3]), [], as_of_date=TODAY)
    chain = [
        q("ABC", EXP_21, "P", 185.0, 3.30, 3.40, -0.24),
        q("ABC", EXP_21, "P", 175.0, 1.20, 1.30, -0.10),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    bps = [x for x in rows if x["strategy"] == "BULL_PUT_SPREAD"]
    assert len(bps) == 1
    assert bps[0]["spread_width"] == 10.0


def test_short_strangle_only_in_positive_gex_high_iv_range():
    b = base_bundle(verdict="mixed", gex_put_wall=94.0, gex_call_wall=106.0, dp_accumulating=None)
    b.volatility.iv_rank = 60.0
    b.volatility.rsi_14 = 50.0
    chain = [
        q("ABC", EXP_35, "P", 92.0, 1.50, 1.60, -0.17),
        q("ABC", EXP_35, "C", 108.0, 1.40, 1.50, 0.16),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    st = [x for x in rows if x["strategy"] == "SHORT_STRANGLE"]
    assert len(st) == 1
    assert st[0]["short_strike"] <= 94.0 and st[0]["leg2_strike"] >= 106.0

    b_neg = base_bundle(verdict="mixed", gex_regime="negative", gex_put_wall=94.0, gex_call_wall=106.0)
    b_neg.volatility.iv_rank = 60.0
    rows_neg = r.generate_opportunities_for_symbol(TODAY, "t", b_neg, chain, [])
    assert not [x for x in rows_neg if x["strategy"] == "SHORT_STRANGLE"]


def test_diagonal_only_in_low_iv_and_no_inversion():
    b = base_bundle()
    b.volatility.iv_rank = 25.0
    chain = [
        q("ABC", EXP_75, "C", 80.0, 21.50, 22.00, 0.78),   # intrinsic 20, extrinsic 2.0 (9%)
        q("ABC", EXP_21 - timedelta(days=7), "C", 112.0, 0.60, 0.65, 0.22),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    dg = [x for x in rows if x["strategy"] == "DIAGONAL_SPREAD"]
    assert len(dg) == 1
    d = dg[0]
    assert d["entry_debit"] == pytest.approx(22.00 - 0.60)
    assert d["entry_debit"] <= 0.78 * (112.0 - 80.0)
    assert d["net_entry_price"] < 0

    b_high = base_bundle()
    b_high.volatility.iv_rank = 55.0
    rows_high = r.generate_opportunities_for_symbol(TODAY, "t", b_high, chain, [])
    assert not [x for x in rows_high if x["strategy"] == "DIAGONAL_SPREAD"]


def test_wheel_cc_is_stateless_and_at_or_above_ceiling():
    b = base_bundle(verdict="mixed")
    b.volatility.rsi_14 = 65.0
    chain = [
        q("ABC", EXP_21, "C", 112.0, 0.95, 1.00, 0.22),
        q("ABC", EXP_21, "C", 105.0, 2.20, 2.30, 0.40),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    cc = [x for x in rows if x["strategy"] == "WHEEL_CC"]
    assert len(cc) == 1
    assert cc[0]["short_strike"] >= 112.0
    # Stateless: bundle has no holding/position attribute at all
    assert not hasattr(b, "position")


def test_wheel_cc_routes_allowed_unknown_category():
    b = base_bundle(watchlist_category="Future-Tier", verdict="mixed")
    b.volatility.rsi_14 = 65.0
    chain = [q("ABC", EXP_21, "C", 112.0, 0.95, 1.00, 0.22)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    cc = [x for x in rows if x["strategy"] == "WHEEL_CC"]
    assert len(cc) == 1
    assert cc[0]["watchlist_category"] == "future-tier"


def test_bear_call_spread_on_bearish_distribution():
    b = base_bundle(verdict="bearish", dp_accumulating=False, bearish_z=1.6)
    chain = [
        q("ABC", EXP_21, "C", 112.0, 1.40, 1.50, 0.24),
        q("ABC", EXP_21, "C", 117.0, 0.35, 0.40, 0.08),
    ]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    bcs = [x for x in rows if x["strategy"] == "BEAR_CALL_SPREAD"]
    assert len(bcs) == 1
    assert bcs[0]["spread_width"] == 5.0


def test_score_bounds_and_band():
    b = base_bundle()
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    for row in rows:
        assert 0 <= row["confidence_score"] <= 100
        assert row["score_band"] in {"STRONG", "HIGHER", "MODERATE", "LOWER"}
        assert set(row["score_components"]) == {
            "jlaw_structure", "flow_darkpool", "gex_corridor", "uw_fundamental",
            "volatility_fit", "liquidity", "economics_timing",
        }


def test_opportunity_key_is_deterministic():
    b = base_bundle()
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    a = r.generate_opportunities_for_symbol(TODAY, "run1", b, chain, [])
    c = r.generate_opportunities_for_symbol(TODAY, "run2", b, chain, [])
    assert [x["opportunity_key"] for x in a] == [x["opportunity_key"] for x in c]


def test_non_us_symbol_skipped():
    b = base_bundle(symbol="0700.HK")
    assert r.generate_opportunities_for_symbol(TODAY, "t", b, [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)], []) == []


def test_direct_euro_category_produces_no_opportunity():
    b = base_bundle(watchlist_category=" Ｅｕｒｏ ")
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    assert r.generate_opportunities_for_symbol(TODAY, "t", b, chain, []) == []


# ---------------------------------------------------------------------------
# Lifecycle tracking
# ---------------------------------------------------------------------------


def _open_row(strategy="WHEEL_CSP", credit=1.80, strike=93.0, exp=EXP_32, **kw):
    row = {
        "opportunity_key": "k",
        "strategy": strategy,
        "status": "PROPOSED",
        "outcome": None,
        "win": None,
        "short_expiry": exp.isoformat(),
        "short_strike": strike,
        "entry_credit": credit,
        "entry_debit": None,
        "spread_width": None,
        "leg2_strike": None,
    }
    row.update(kw)
    return row


def test_lifecycle_50pct_profit_auto_win():
    row = _open_row()
    sq = q("ABC", EXP_32, "P", 93.0, 0.80, 0.85, -0.12)
    out = r.evaluate_open_opportunity_row(row, TODAY + timedelta(days=5), 104.0, sq)
    assert out["outcome"] == "WON" and out["win"] is True and out["status"] == "CLOSED"


def test_lifecycle_21dte_mark_and_gex_breach():
    row = _open_row()
    as_of = EXP_32 - timedelta(days=20)
    sq = q("ABC", EXP_32, "P", 93.0, 1.40, 1.50, -0.25)
    out = r.evaluate_open_opportunity_row(row, as_of, 96.0, sq, current_gex_put_wall=90.0)
    assert out["dte21_mark_date"] == as_of.isoformat()
    assert out["dte21_pnl"] == pytest.approx(1.80 - 1.50)
    assert out["defense_reason"] == "GEX_PUT_WALL_BREACH"
    assert out["outcome"] is None


def test_lifecycle_expiry_settlement_bps_loss():
    row = _open_row(strategy="BULL_PUT_SPREAD", credit=1.0, strike=93.0, spread_width=5.0, leg2_strike=88.0)
    out = r.evaluate_open_opportunity_row(row, EXP_32, 85.0, None)
    assert out["outcome"] == "LOST" and out["win"] is False
    assert out["exit_pnl"] == pytest.approx(1.0 - 5.0)


def test_lifecycle_expiry_worthless_win():
    row = _open_row()
    out = r.evaluate_open_opportunity_row(row, EXP_32, 101.0, None)
    assert out["outcome"] == "EXPIRED_WORTHLESS" and out["win"] is True


def test_lifecycle_terminal_rows_untouched():
    row = _open_row(outcome="WON", win=True, status="CLOSED")
    assert r.evaluate_open_opportunity_row(row, EXP_32, 50.0, None) == row


# ---------------------------------------------------------------------------
# Report & compliance
# ---------------------------------------------------------------------------


def test_markdown_report_renders():
    b = base_bundle()
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)], [])
    md = r.render_markdown_report(TODAY, rows)
    assert "Ranked Opportunity Board" in md and "ABC" in md
    assert "No symbols passed" in r.render_markdown_report(TODAY, [])


WHITELIST_PATTERNS = [
    r"^/api/option-trades$",
    r"^/api/stock/\{ticker\}/option-contracts$",
    r"^/api/stock/\{ticker\}/interpolated-iv$",
    r"^/api/stock/\{ticker\}/technical-indicator/RSI$",
    r"^/api/stock/\{ticker\}/earnings$",
    r"^/api/stock/\{ticker\}/financials$",
]


def test_runner_only_calls_whitelisted_uw_endpoints_with_get():
    src = (SCRIPTS_DIR / "uw_options_income_runner.py").read_text(encoding="utf-8")
    calls = re.findall(r'self\._get\(\s*f?"([^"]+)"', src)
    assert calls, "expected UW endpoint calls"
    for path in calls:
        assert any(re.match(p, path) for p in WHITELIST_PATTERNS), f"non-whitelisted endpoint: {path}"
    assert "UW-CLIENT-API-ID" in src and '"100001"' in src
    assert "https://api.unusualwhales.com" in src
    uw_client_src = src.split("class UnusualWhalesClient")[1].split("class SupabaseRestClient")[0]
    assert ".post(" not in uw_client_src and ".patch(" not in uw_client_src and ".delete(" not in uw_client_src
    assert "/api/v1/" not in src and "/api/v2/" not in src


def test_runner_never_reads_holdings():
    src = (SCRIPTS_DIR / "uw_options_income_runner.py").read_text(encoding="utf-8")
    assert "position" not in re.sub(r"#.*", "", src).replace("watchlist.position", "")  # stateless guarantee


# ---------------------------------------------------------------------------
# Watchlist universe (all non-euro US-option-listed categories)
# ---------------------------------------------------------------------------


WL_ROWS = [
    {"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
    {"symbol": "avgo ", "category": " Satellite", "exchange": "NASDAQ"},
    {"symbol": "TSM", "category": "watch1", "exchange": "NYSE"},
    {"symbol": "SPY", "category": "core", "exchange": "AMEX"},
    {"symbol": "AMD", "category": "watch1", "exchange": "NASDAQ"},
    {"symbol": "PLTR", "category": " watch2 ", "exchange": "NASDAQ"},
    {"symbol": "RKLB", "category": "WATCH3", "exchange": "NASDAQ"},
    {"symbol": "FUTR", "category": "Future_Tier", "exchange": "BATS"},
    {"symbol": "SAP", "category": " Euro ", "exchange": "NASDAQ"},
    {"symbol": "ASML", "category": "Ｅｕｒｏ", "exchange": "NYSE"},
    {"symbol": "BESIY", "category": "watch1", "exchange": "OTC"},
    {"symbol": "LSECO", "category": "watch2", "exchange": "LSE"},
    {"symbol": "STOCK", "category": "watch3", "exchange": "Stockholm"},
    {"symbol": "XETRA", "category": "future", "exchange": "XETR"},
    {"symbol": "NOCAT", "category": None, "exchange": "NASDAQ"},
    {"symbol": "BLANKCAT", "category": "  ", "exchange": "NYSE"},
    {"symbol": "NOEX", "category": "watch2", "exchange": ""},
    {"symbol": "MISSINGEX", "category": "watch3"},
    {"symbol": "TSM", "category": "euro", "exchange": "NASDAQ"},  # must not suppress eligible duplicate
    {"symbol": "TSM", "category": "core", "exchange": "NYSE"},  # duplicate -> keep core
]


def test_category_policy_is_canonical_and_rejects_only_blank_or_euro():
    assert r.normalize_watchlist_category(" Ｅｕｒｏ ") == "euro"
    assert not r.is_eligible_watchlist_category(None)
    assert not r.is_eligible_watchlist_category("  ")
    assert not r.is_eligible_watchlist_category(" Euro ")
    assert r.is_eligible_watchlist_category("watch2")
    assert r.is_eligible_watchlist_category("future-tier")


def test_universe_includes_all_non_euro_us_listed_categories():
    u = r.build_watchlist_universe(WL_ROWS)
    assert u == [
        ("NVDA", "core"), ("SPY", "core"), ("TSM", "core"), ("AVGO", "satellite"),
        ("AMD", "watch1"), ("FUTR", "future_tier"), ("PLTR", "watch2"), ("RKLB", "watch3"),
    ]
    syms = {s for s, _ in u}
    assert {"PLTR", "RKLB", "FUTR"} <= syms
    assert not syms & {
        "SAP", "ASML", "BESIY", "LSECO", "STOCK", "XETRA", "NOCAT", "BLANKCAT", "NOEX", "MISSINGEX",
    }


def test_universe_dedup_is_legacy_priority_then_lexical_and_input_order_independent():
    rows = [
        {"symbol": "LEGACY", "category": "watch1", "exchange": "NASDAQ"},
        {"symbol": "LEGACY", "category": "satellite", "exchange": "NASDAQ"},
        {"symbol": "LEGACY", "category": "core", "exchange": "NASDAQ"},
        {"symbol": "FALLBK", "category": "zeta", "exchange": "NYSE"},
        {"symbol": "FALLBK", "category": "alpha", "exchange": "NYSE"},
        {"symbol": "ELIGIB", "category": "euro", "exchange": "NASDAQ"},
        {"symbol": "ELIGIB", "category": "core", "exchange": "LSE"},
        {"symbol": "ELIGIB", "category": "watch3", "exchange": "NASDAQ"},
    ]
    expected = [("LEGACY", "core"), ("FALLBK", "alpha"), ("ELIGIB", "watch3")]
    assert r.build_watchlist_universe(rows) == expected
    assert r.build_watchlist_universe(list(reversed(rows))) == expected


def test_symbol_override_can_only_narrow_universe():
    u = r.build_watchlist_universe(WL_ROWS, ["nvda", "FUTR", "PLTR", "SAP", "AAPL"])
    assert u == [("NVDA", "core"), ("FUTR", "future_tier"), ("PLTR", "watch2")]


def test_fetch_watchlist_rows_paginates_all_rows_with_stable_parameters():
    calls = []

    class FakeSupabase:
        def select(self, table, params=None):
            calls.append((table, dict(params or {})))
            if params["offset"] == "0":
                return [
                    {"symbol": f"SKIP{i}", "category": "euro", "exchange": "NASDAQ"}
                    for i in range(r.WATCHLIST_PAGE_SIZE)
                ]
            if params["offset"] == str(r.WATCHLIST_PAGE_SIZE):
                return [{"symbol": "LATE", "category": " Watch3 ", "exchange": "NYSE"}]
            raise AssertionError(f"unexpected offset: {params['offset']}")

    rows = r.fetch_watchlist_rows(FakeSupabase())
    assert r.build_watchlist_universe(rows) == [("LATE", "watch3")]
    assert calls == [
        (
            "watchlist",
            {
                "select": "symbol,category,exchange",
                "order": "symbol.asc,category.asc,exchange.asc",
                "limit": "1000",
                "offset": "0",
            },
        ),
        (
            "watchlist",
            {
                "select": "symbol,category,exchange",
                "order": "symbol.asc,category.asc,exchange.asc",
                "limit": "1000",
                "offset": "1000",
            },
        ),
    ]


def test_fetch_watchlist_rows_stops_after_malformed_response():
    class FakeSupabase:
        def __init__(self):
            self.calls = 0

        def select(self, _table, params=None):
            self.calls += 1
            return {"not": "a page"}

    sb = FakeSupabase()
    assert r.fetch_watchlist_rows(sb) == []
    assert sb.calls == 1


# ---------------------------------------------------------------------------
# Combined unusual-flow score
# ---------------------------------------------------------------------------


def _sig(d, net_mm, unu_mm, dc):
    return {"signal_date": d, "net_directional_premium": net_mm * 1e6, "unusual_premium": unu_mm * 1e6, "directional_confidence": dc}


def test_premium_tilt_is_per_ticker_and_bounded():
    assert r._premium_tilt(_sig("2026-10-28", 447.6, 520.3, 0.74)) == pytest.approx(0.74, abs=1e-3)
    assert r._premium_tilt(_sig("2026-10-28", -103.8, 112.5, 0.53)) == pytest.approx(-0.53, abs=1e-3)
    small = r._premium_tilt(_sig("2026-10-28", 5.2, 5.05, 1.0))
    assert 0.3 <= small < 0.4  # $5M strong-but-small print is down-weighted
    assert r._premium_tilt(_sig("2026-10-28", 3.5, 1.5, 0.0)) == 0.0  # failed evidence gate


def test_flow_composite_flags_mu_style_verdict_conflict():
    # 2026-10-28 MU: verdict bearish (20 alerts) but +$448M bullish unusual premium
    b = base_bundle(verdict="bearish", flow_alert_count=20, dp_total_premium_mm=180.7, dp_net_mm=-5.1)
    b.flow_history = [_sig("2026-10-28", 447.6, 520.3, 0.74), _sig("2026-10-27", 97.1, 198.9, 0.39), _sig("2026-10-26", 104.2, 240.8, 0.37)]
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert "verdict_vs_premium" in b.flow_conflicts
    assert b.flow_label == "neutral"
    assert -15 < b.flow_composite < 15


def test_flow_composite_flags_darkpool_conflict_meta_style():
    b = base_bundle(verdict="bullish", flow_alert_count=20, dp_total_premium_mm=3013.2, dp_net_mm=-2847.6)
    b.flow_history = [_sig("2026-10-28", -103.8, 112.5, 0.53)]
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert "darkpool_vs_options" in b.flow_conflicts or "verdict_vs_premium" in b.flow_conflicts
    assert b.flow_label in {"neutral", "bear"}


def test_flow_composite_strong_bull_when_all_agree():
    b = base_bundle(verdict="bullish", flow_alert_count=20, dp_total_premium_mm=768.3, dp_net_mm=401.8)
    b.flow_history = [_sig("2026-10-28", 49.9, 42.0, 0.9), _sig("2026-10-27", 38.1, 59.4, 0.61), _sig("2026-10-26", -12.1, 20.3, 0.35)]
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert b.flow_label == "strong_bull"
    assert not b.flow_conflicts


def test_flow_composite_ignores_stale_signals_and_reweights():
    b = base_bundle(verdict="bullish", flow_alert_count=10, dp_total_premium_mm=0.0, dp_net_mm=None)
    b.flow_history = [_sig("2026-10-13", -300, 400, 0.9)]
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert "premium_today" in b.flow_components["missing"]
    assert b.flow_composite == pytest.approx(100.0, abs=0.1)  # only the verdict leg remains


def test_darkpool_alone_cannot_set_label():
    b = base_bundle(verdict="no-data", dp_total_premium_mm=500.0, dp_net_mm=-500.0)
    b.flow_history = []
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert b.flow_label == "neutral"
    assert abs(b.flow_composite) <= 15.0


def test_strong_bearish_flow_vetoes_short_put_structures():
    b = base_bundle()
    b.flow_alert_count = 20
    b.verdict = "mixed"
    b.dp_total_premium_mm, b.dp_net_mm = 500.0, -480.0
    b.flow_history = [_sig(TODAY.isoformat(), -400, 450, 0.9), _sig("2026-10-16", -300, 400, 0.8), _sig("2026-10-15", -200, 300, 0.7)]
    r.compute_flow_composite(b, TODAY)
    assert b.flow_composite <= -r.FLOW_STRONG
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    assert not [x for x in rows if x["strategy"] in {"WHEEL_CSP", "BULL_PUT_SPREAD"}]


def test_flow_points_follow_strategy_direction():
    assert r.flow_alignment("WHEEL_CSP", 60) == pytest.approx(0.6)
    assert r.flow_alignment("BEAR_CALL_SPREAD", 60) == pytest.approx(-0.6)
    assert r.flow_alignment("SHORT_STRANGLE", 0) == pytest.approx(1.0)
    assert r.flow_alignment("SHORT_STRANGLE", 50) == pytest.approx(-1.0)


def test_rows_carry_flow_fields_and_conflict_caution():
    b = base_bundle(verdict="bullish", flow_alert_count=20)
    b.flow_history = [_sig(TODAY.isoformat(), -60, 80, 0.8)]
    r.compute_flow_composite(b, TODAY)
    chain = [q("ABC", EXP_32, "P", 93.0, 1.85, 1.95, -0.24)]
    rows = r.generate_opportunities_for_symbol(TODAY, "t", b, chain, [])
    assert rows, "expected at least one candidate"
    row = rows[0]
    for k in ("flow_composite", "flow_label", "flow_conflicts", "flow_components", "flow_alert_count"):
        assert k in row
    assert "FLOW_CONFLICT_VERDICT_VS_PREMIUM" in (row["caution"] or "")
    md = r.render_markdown_report(TODAY, rows, [])
    assert "Flow Conflicts" in md


# ---------------------------------------------------------------------------
# Verdict freshness + uncapped alert stats (2026-10-29)
# ---------------------------------------------------------------------------


def test_stale_verdict_is_dropped_from_composite():
    b = base_bundle(verdict="bullish", flow_alert_count=20, dp_total_premium_mm=0.0, dp_net_mm=None)
    b.verdict_date = "2026-10-20"
    b.flow_history = [_sig("2026-10-28", 10.0, 20.0, 0.5)]
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert b.verdict_stale is True
    assert "verdict" in b.flow_components["missing"]
    assert b.flow_components["verdict_date"] == "2026-10-20"


def test_friday_verdict_still_fresh_on_tuesday():
    assert r.verdict_is_stale("2026-10-23", date(2026, 10, 27)) is False
    assert r.verdict_is_stale("2026-10-23", date(2026, 10, 28)) is True
    assert r.verdict_is_stale(None, date(2026, 10, 28)) is True


def test_uncapped_stats_weight_the_verdict_leg():
    thin = base_bundle(verdict="bullish", flow_alert_count=3, dp_total_premium_mm=0.0, dp_net_mm=None)
    thin.verdict_date, thin.alert_count_total, thin.alert_premium_tilt = "2026-10-29", 3, 0.1
    deep = base_bundle(verdict="bullish", flow_alert_count=20, dp_total_premium_mm=0.0, dp_net_mm=None)
    deep.verdict_date, deep.alert_count_total, deep.alert_premium_tilt = "2026-10-29", 240, 0.8
    r.compute_flow_composite(thin, date(2026, 10, 29))
    r.compute_flow_composite(deep, date(2026, 10, 29))
    assert thin.flow_components["verdict"] == pytest.approx(0.5 * 0.3 + 0.5 * 0.1, abs=1e-3)
    assert deep.flow_components["verdict"] == pytest.approx(0.9, abs=1e-3)
    assert deep.flow_composite > thin.flow_composite


def test_verdict_vs_alert_premium_conflict():
    b = base_bundle(verdict="bearish", flow_alert_count=20, dp_total_premium_mm=0.0, dp_net_mm=None)
    b.verdict_date, b.verdict_quant, b.alert_count_total, b.alert_premium_tilt = "2026-10-29", "bullish", 300, 0.6
    r.compute_flow_composite(b, date(2026, 10, 29))
    assert "verdict_vs_alert_premium" in b.flow_conflicts
    assert b.flow_components["verdict_quant"] == "bullish"


class RunnerTestSuite(unittest.TestCase):
    pass


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(RunnerTestSuite, _name, staticmethod(_fn))
del _name, _fn


if __name__ == "__main__":
    unittest.main(verbosity=2)
