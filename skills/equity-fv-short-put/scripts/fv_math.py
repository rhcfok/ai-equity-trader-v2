"""Arithmetic helpers for the equity-fv-short-put skill.

Import in a scratch script, or run `python -I fv_math.py --demo` to see the output shape.
All money is in the same unit per call (usually $ per share).
"""


def pe(price, eps):
    return price / eps if eps else None


def eps_from_pe(price, forward_pe):
    """Implied forward EPS from a vendor forward P/E."""
    return price / forward_pe if forward_pe else None


def fv(eps, multiple):
    return eps * multiple


def triple(spot, eps, base_mult, bull_mult=None, bear_mult=None,
           bull_eps=None, bear_eps=None, weights=(0.25, 0.50, 0.25)):
    """Bull/base/bear fair value and probability-weighted value.

    bull_eps / bear_eps let the bear case haircut earnings, not just the multiple.
    """
    base = fv(eps, base_mult)
    bull = fv(bull_eps if bull_eps is not None else eps,
              bull_mult if bull_mult is not None else base_mult * 1.15)
    bear = fv(bear_eps if bear_eps is not None else eps,
              bear_mult if bear_mult is not None else base_mult * 0.75)
    pw = weights[0] * bull + weights[1] * base + weights[2] * bear
    return dict(base=base, bull=bull, bear=bear, pw=pw,
                vs_spot=(pw / spot - 1) * 100,
                base_vs_spot=(base / spot - 1) * 100,
                pe_base=pe(spot, eps))


def put_econ(fv_value, strike, bid, dte, spot):
    static = bid / strike * 100
    be = strike - bid
    return dict(
        static=static,
        ann=static * 365 / dte,
        be=be,
        buf_k=(fv_value - strike) / fv_value * 100,
        buf_be=(fv_value - be) / fv_value * 100,
        otm=(spot - strike) / spot * 100,
        collateral=strike * 100,
        credit=bid * 100,
    )


def ev(market_cap, net_debt):
    return market_cap + net_debt


def ev_sales(market_cap, net_debt, revenue):
    return ev(market_cap, net_debt) / revenue if revenue else None


def demo():
    names = [
        # ticker, spot, fy1_eps, base_mult, bull_mult, bear_mult, bear_eps
        ("MSFT", 488.32, 20.01, 25.0, 28.5, 19.5, None),
        ("GOOGL", 279.50, 14.89, 18.8, 22.0, 14.0, None),
        ("AMAT", 330.42, 19.30, 20.0, 24.0, 13.0, 14.0),
    ]
    print(f"{'T':6}{'Spot':>8}{'FY1EPS':>8}{'P/E':>7}{'Base':>8}{'Bull':>8}{'Bear':>8}{'PW':>8}{'vsSpot':>8}")
    for t, s, e, bm, um, dm, de in names:
        r = triple(s, e, bm, um, dm, bear_eps=de)
        print(f"{t:6}{s:8.2f}{e:8.2f}{r['pe_base']:7.1f}{r['base']:8.1f}{r['bull']:8.1f}"
              f"{r['bear']:8.1f}{r['pw']:8.1f}{r['vs_spot']:+8.1f}%")
    print()
    p = put_econ(385, 320, 21.15, 52, 330.42)
    print("AMAT Jan 320P:", {k: round(v, 2) for k, v in p.items()})


if __name__ == "__main__":
    demo()
