"""
Offline unit tests for uw_options_income_runner.py (no network, no credentials).

Run:
    python -m pytest skills/uw-options-income-engine/tests -q
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
# Watchlist universe (core / satellite / watch1 only)
# ---------------------------------------------------------------------------


WL_ROWS = [
    {"symbol": "NVDA", "category": "core", "exchange": "NASDAQ"},
    {"symbol": "avgo ", "category": " Satellite", "exchange": "NASDAQ"},
    {"symbol": "TSM", "category": "watch1", "exchange": "NYSE"},
    {"symbol": "SPY", "category": "core", "exchange": "AMEX"},
    {"symbol": "PLTR", "category": "watch2", "exchange": "NASDAQ"},
    {"symbol": "RKLB", "category": "watch3", "exchange": "NASDAQ"},
    {"symbol": "SAP", "category": "euro", "exchange": "XETR"},
    {"symbol": "BESIY", "category": "watch1", "exchange": "OTC"},
    {"symbol": "IQE.L", "category": "watch1", "exchange": "LSE"},
    {"symbol": "SIVE.ST", "category": "watch1", "exchange": "Stockholm"},
    {"symbol": "TSM", "category": "core", "exchange": "NYSE"},  # duplicate -> keep core
]


def test_universe_limited_to_core_satellite_watch1_us_listed():
    u = r.build_watchlist_universe(WL_ROWS)
    assert u == [("NVDA", "core"), ("SPY", "core"), ("TSM", "core"), ("AVGO", "satellite")]
    syms = {s for s, _ in u}
    assert not syms & {"PLTR", "RKLB", "SAP", "BESIY", "IQE.L", "SIVE.ST"}


def test_symbol_override_can_only_narrow_universe():
    u = r.build_watchlist_universe(WL_ROWS, ["nvda", "PLTR", "AAPL"])
    assert u == [("NVDA", "core")]  # PLTR is watch2, AAPL not on watchlist


def test_main_queries_watchlist_with_server_side_category_filter():
    src = (SCRIPTS_DIR / "uw_options_income_runner.py").read_text(encoding="utf-8")
    assert '"category": f"in.({\',\'.join(WATCHLIST_CATEGORIES)})"' in src
    assert r.WATCHLIST_CATEGORIES == ("core", "satellite", "watch1")


class RunnerTestSuite(unittest.TestCase):
    pass


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(RunnerTestSuite, _name, staticmethod(_fn))
del _name, _fn


if __name__ == "__main__":
    unittest.main(verbosity=2)

