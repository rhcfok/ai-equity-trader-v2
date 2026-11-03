"""Shared EM probability model — the single source of truth for the math,
paths, and credentials used by every script in this skill.

Model (calibrated drift-off lognormal; see ../references/methodology.md):
  - ln(S_T) ~ N(ln(S0) + h*mu, (sigma*sqrt(h))^2); the CALIBRATED
    configuration sets mu = 0 (drift-off). The Pine-indicator default
    (SMA-slope drift) degraded 30d coverage from 64.5% to 54.3% and its
    down-calls hit only 43.8% — never route on it.
  - 64.5% of 30d outcomes inside +/-1 sigma -> +/-1.12 sigma is the
    empirical 68% band (Z68).
Every consumer imports from here so the math exists in exactly one place.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd

MODEL_VERSION = "gaussian-drift-off-v1"
Z68 = 1.12  # empirical 68% band multiplier at ~30 trading days

# --- paths ---------------------------------------------------------------
# scripts/ -> em-expected-move/ -> skills/ -> repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
DATA_DIR = os.environ.get("EM_DATA_DIR") or os.path.join(REPO_ROOT, "out")


def load_creds() -> tuple[str, str]:
    """SUPABASE_URL / SUPABASE_KEY env vars, else the repo-root .env."""
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
        return os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    env = {}
    with open(os.path.join(REPO_ROOT, ".env")) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    return env["SUPABASE_URL"], env["SUPABASE_KEY"]


# --- math ----------------------------------------------------------------
def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma50(ser: pd.Series) -> float:
    """Per-bar LOG-return stdev over the last 50 sessions (population, Pine-style)."""
    ser = ser.dropna()
    ratio = ser / ser.shift(1)
    lr = np.log(ratio)
    lr = lr[(ratio - 1).abs() < 0.5]  # drop bad prints (simple return beyond +/-50%)
    return float(lr.iloc[-50:].std(ddof=0))


def p_win_expiry(s0: float, k: float, h: int, is_put: bool, sigma_d: float) -> float:
    """P(short leg finishes OTM at expiry), drift-off lognormal."""
    vol_h = sigma_d * math.sqrt(max(1, h))
    z = math.log(k / s0) / vol_h
    return phi(-z) if is_put else phi(z)


def p_profit50_first(s0: float, k: float, delta: float, credit: float) -> float | None:
    """P(price banks 50% of the entry credit before the short strike is touched).

    Driftless two-barrier approximation in price space: the profit barrier
    distance is the linear-delta move that halves the premium
    (0.5*credit/|delta|), the breach barrier is the short strike.
    None when delta/credit are unusable. Credit must be PER SHARE.
    """
    if credit <= 0 or delta <= 0:
        return None
    d_breach = abs(s0 - k)
    d_profit = 0.5 * credit / delta
    if d_breach + d_profit <= 0:
        return None
    return d_breach / (d_breach + d_profit)


def outside_band112(s0: float, k: float, h: int, is_put: bool, sigma_d: float) -> bool:
    """True when the strike sits beyond the empirical 68% band."""
    vol_h = sigma_d * math.sqrt(max(1, h))
    edge = s0 * math.exp(Z68 * vol_h)
    return k < s0 * math.exp(-Z68 * vol_h) if is_put else k > edge
