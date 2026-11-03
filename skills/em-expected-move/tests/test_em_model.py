"""Offline unit tests for em_model.py (no network, no Supabase).

Run: python skills/em-expected-move/tests/test_em_model.py
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from em_model import (  # noqa: E402
    Z68,
    bcs_fair_credit,
    bs_price,
    model_delta,
    outside_band112,
    p_profit50_first,
    p_win_expiry,
    phi,
    sigma50,
)

failures = []


def check(name, cond):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        failures.append(name)


# ---- phi -------------------------------------------------------------------
print("phi")
check("phi(0) == 0.5", abs(phi(0.0) - 0.5) < 1e-12)
check("phi(1.96) ~ 0.975", abs(phi(1.96) - 0.9750021) < 1e-4)
check("phi(-x) = 1 - phi(x)", abs(phi(-1.2) + phi(1.2) - 1.0) < 1e-12)

# ---- sigma50 ---------------------------------------------------------------
print("sigma50")
rng = np.random.default_rng(7)
prices = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
ser = pd.Series(prices, index=pd.bdate_range("2026-01-01", periods=200))
lr = np.diff(np.log(prices))
check("matches np.std(ddof=0) of last 50 log returns",
      abs(sigma50(ser) - lr[-50:].std(ddof=0)) < 1e-12)
ser_bad = ser.copy()
ser_bad.iloc[-1] = ser_bad.iloc[-2] * 3  # 200% one-day print -> ratio guard
lr_bad = np.diff(np.log(ser_bad.values))
lr_clean = lr_bad[np.abs(lr_bad) < math.log(1.5)]
check("bad prints filtered", abs(sigma50(ser_bad) - lr_clean[-50:].std(ddof=0)) < 1e-9)

# ---- p_win_expiry ----------------------------------------------------------
print("p_win_expiry")
s0, k, h, sig = 100.0, 90.0, 30, 0.02
p_put = p_win_expiry(s0, k, h, is_put=True, sigma_d=sig)
check("OTM put > 50%", p_put > 0.5)
check("OTM put matches Phi(ln(S0/K)/vol)",
      abs(p_put - phi(math.log(s0 / k) / (sig * math.sqrt(h)))) < 1e-12)
p_call = p_win_expiry(s0, 110.0, 30, is_put=False, sigma_d=sig)
check("OTM call matches Phi(ln(K/S0)/vol)",
      abs(p_call - phi(math.log(110.0 / s0) / (sig * math.sqrt(h)))) < 1e-12)
check("ATM -> 50%", abs(p_win_expiry(100.0, 100.0, 30, True, sig) - 0.5) < 1e-12)
check("far OTM put > near OTM put",
      p_win_expiry(100, 80, 30, True, sig) > p_put)
check("call/put mirrored log-strikes equal",
      abs(p_win_expiry(100, 110, 30, False, sig)
          - p_win_expiry(100, 100 * 100 / 110, 30, True, sig)) < 1e-12)

# ---- p_profit50_first ------------------------------------------------------
print("p_profit50_first")
# s0=100, K=90 (d_b=10), credit=2, delta=0.2 -> d_p = 0.5*2/0.2 = 5 -> 10/15
check("two-barrier ratio", abs(p_profit50_first(100, 90, 0.2, 2.0) - 10 / 15) < 1e-12)
check("delta 0 -> None", p_profit50_first(100, 90, 0.0, 2.0) is None)
check("credit 0 -> None", p_profit50_first(100, 90, 0.2, 0.0) is None)
check("credit negative -> None", p_profit50_first(100, 90, 0.2, -1.0) is None)
check("closer strike lowers p50",
      p_profit50_first(100, 95, 0.2, 2.0) < p_profit50_first(100, 90, 0.2, 2.0))

# ---- outside_band112 -------------------------------------------------------
print("outside_band112")
edge = 100.0 * math.exp(Z68 * sig * math.sqrt(30))
check("call just beyond 1.12sd -> True", outside_band112(100.0, edge * 1.001, 30, False, sig))
check("call just inside 1.12sd -> False",
      not outside_band112(100.0, edge * 0.999, 30, False, sig))
check("put beyond lower 1.12sd -> True",
      outside_band112(100.0, 100.0 * 100.0 / edge * 0.999, 30, True, sig))

# ---- bs_price / model_delta / bcs_fair_credit ------------------------------
print("bs_price / model_delta / bcs_fair_credit")
s0b, kb, hb, sigb = 100.0, 100.0, 30, 0.02
call = bs_price(s0b, kb, hb, False, sigb)
put = bs_price(s0b, kb, hb, True, sigb)
check("put-call parity (r=0): C - P = S0 - K", abs((call - put) - (s0b - kb)) < 1e-9)
check("call price positive, decreasing in strike",
      bs_price(100, 90, 30, False, sigb) > call > bs_price(100, 110, 30, False, sigb) > 0)
check("ATM model |delta| ~ 0.5+ (slightly above)",
      abs(model_delta(s0b, kb, hb, False, sigb) - phi(0.5 * sigb * math.sqrt(hb))) < 1e-12)
check("put |delta| = call delta at same strike? (no) put delta < call delta",
      model_delta(s0b, kb, hb, True, sigb) < model_delta(s0b, kb, hb, False, sigb))
fc = bcs_fair_credit(100.0, 105.0, 110.0, 30, False, sigb)
check("call-spread fair credit in (0, width)",
      0 < fc < 5.0
      and abs(fc - (bs_price(100, 105, 30, False, sigb) - bs_price(100, 110, 30, False, sigb))) < 1e-12)
fp = bcs_fair_credit(100.0, 95.0, 90.0, 30, True, sigb)
check("put-spread fair credit = long P(90) - short P(95) > 0", 0 < fp <= 5.0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
