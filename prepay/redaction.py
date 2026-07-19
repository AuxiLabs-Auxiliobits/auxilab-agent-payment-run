"""Identifier-redaction safety layer.

This is the core auditability guarantee of the agent:

    The LLM never validates payments and never sees transaction-level
    identifiers. It only receives AGGREGATE metadata.

``build_narrative_metadata`` converts the scored result into a dictionary that
contains only counts, totals and percentages - no payment ids, vendor names,
invoice numbers, bank routing or approver names ever cross this boundary.
``assert_redacted`` is a defensive check used in tests and at runtime to prove
the payload is clean before it is handed to any model provider.
"""
from __future__ import annotations

from typing import Iterable, List

from . import config
from .schema import VIOLATION_LABELS

# Keys that identify a transaction/vendor and must NEVER reach the LLM.
FORBIDDEN_KEYS = {
    "payment_id", "vendor", "vendor_id", "vendor_name", "invoice_number",
    "bank_routing", "approved_bank_routing", "authorizer", "approver_name",
    "reason", "reasons", "flags",
}


def build_narrative_metadata(score: dict, provided_batch_id: str = "") -> dict:
    """Aggregate-only payload safe to pass to an LLM."""
    counts = score.get("counts_by_type", {})

    def _c(vtype: str) -> int:
        return int(counts.get(vtype, {}).get("count", 0))

    discount_missed = _c("MISSED_EARLY_PAYMENT_DISCOUNT")
    discount_available = _c("EARLY_PAYMENT_DISCOUNT_AVAILABLE")

    flag_summary = {
        VIOLATION_LABELS.get(vtype, vtype): int(info["count"])
        for vtype, info in counts.items()
    }

    metadata = {
        "batch_id": provided_batch_id or "payment-batch",
        "currency": config.CURRENCY,
        "payment_count": score["payment_count"],
        "total_batch_value": score["total_value"],
        "clear_count": score["clear_count"],
        "review_count": score["review_count"],
        "hold_count": score["hold_count"],
        "flagged_count": score["flagged_count"],
        "flagged_value": score["flagged_value"],
        "hold_value": score["hold_value"],
        "high_risk_exposure": score["hold_value"],
        "integrity_score": score["integrity_score"],
        "decision": score["decision"],
        "red_flags": score["red_flags"],
        "yellow_flags": score["yellow_flags"],
        "duplicate_count": _c("DUPLICATE_PAYMENT"),
        "unapproved_count": _c("UNAPPROVED_PAYMENT"),
        "invalid_vendor_count": _c("INVALID_VENDOR") + _c("INACTIVE_VENDOR"),
        "amount_mismatch_count": _c("AMOUNT_MISMATCH"),
        "unregistered_invoice_count": _c("UNREGISTERED_INVOICE"),
        "bank_routing_mismatch_count": _c("BANK_ROUTING_MISMATCH"),
        "overdue_count": _c("OVERDUE_PAYMENT"),
        "discount_missed_count": discount_missed,
        "discount_available_count": discount_available,
        "discount_opportunities": discount_missed + discount_available,
        "flag_summary": flag_summary,
    }
    return metadata


def assert_redacted(metadata: dict) -> None:
    """Raise if any forbidden identifier key leaked into the payload."""
    leaked = _find_forbidden_keys(metadata)
    if leaked:
        raise ValueError(
            f"Redaction violation: identifier keys reached the LLM payload: {leaked}"
        )


def _find_forbidden_keys(obj, path: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                found.append(f"{path}{k}")
            found.extend(_find_forbidden_keys(v, f"{path}{k}."))
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            found.extend(_find_forbidden_keys(item, f"{path}[{i}]."))
    return found
