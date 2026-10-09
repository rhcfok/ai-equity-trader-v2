"""Shared contracts for Unusual Whales option-breakout enrichment of trade-jlaw-v2."""
from __future__ import annotations

import math
from typing import Any

OPTION_FIELD_MAP = {
    "option_score": "total_score",
    "option_tier": "tier",
    "option_alert": "alert",
    "option_a1": "a1",
    "option_a2": "a2",
    "option_a3": "a3",
    "option_a4": "a4",
    "option_a5": "a5",
    "option_a6": "a6",
    "option_a7": "a7",
    "option_a8": "a8",
    "option_iv30": "iv30",
    "option_rv20": "rv20",
    "option_iv_rank": "iv_rank",
    "option_skew_rank": "skew_rank",
    "option_put_call_ratio": "pc_ratio",
    "option_iv_trend": "iv_trend",
    "option_pct_below_high": "pct_below_high",
}

OPTION_COLUMNS = tuple(OPTION_FIELD_MAP)
IDENTITY_COLUMNS = ("id", "run_date", "symbol", "jlaw_review_type")


class OptionContractError(ValueError):
    """Raised when an option enrichment record does not match the table contract."""


def finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def option_fields_from_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Map a provider analysis result to the 18 nullable trade-jlaw-v2 fields."""
    missing = [source for source in OPTION_FIELD_MAP.values() if source not in analysis]
    if missing:
        raise OptionContractError(f"Analysis is missing required option field(s): {sorted(set(missing))}")

    values: dict[str, Any] = {}
    for target, source in OPTION_FIELD_MAP.items():
        value = analysis[source]
        if target in {"option_tier", "option_iv_trend"}:
            values[target] = str(value) if value is not None else None
        elif target in {"option_alert", *[f"option_a{i}" for i in range(1, 9)]}:
            values[target] = bool(value) if value is not None else None
        else:
            values[target] = finite_or_none(value)
    return values


def validate_update_record(record: dict[str, Any]) -> None:
    """Validate an update record before it can be applied to Supabase."""
    missing_identity = [field for field in IDENTITY_COLUMNS if record.get(field) in (None, "")]
    missing_options = [field for field in OPTION_COLUMNS if field not in record]
    if missing_identity or missing_options:
        fragments = []
        if missing_identity:
            fragments.append(f"identity={missing_identity}")
        if missing_options:
            fragments.append(f"option_fields={missing_options}")
        raise OptionContractError("Invalid option update record: " + "; ".join(fragments))


def db_patch_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return only whitelisted option fields; never patch JLaw source fields."""
    validate_update_record(record)
    return {field: record[field] for field in OPTION_COLUMNS}
