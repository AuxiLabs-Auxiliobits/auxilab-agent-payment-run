"""Scoring & per-payment status classification.

Turns the flat list of violations into:
  * a per-payment status (Clear / Review Required / Hold)
  * batch-level counts and financial exposure
  * a value-weighted integrity score and an overall decision
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

import pandas as pd

from .schema import (
    BLOCKING_VIOLATIONS,
    INFO_VIOLATIONS,
    REVIEW_VIOLATIONS,
    SEVERITY_RED,
    SEVERITY_YELLOW,
    STATUS_CLEAR,
    STATUS_HOLD,
    STATUS_REVIEW,
    VIOLATION_LABELS,
    status_for_types,
)


def build_payment_status(batch: pd.DataFrame, violations: List[dict]) -> pd.DataFrame:
    """Return one row per payment with its status and attached flags."""
    by_payment: Dict[str, List[dict]] = defaultdict(list)
    for v in violations:
        by_payment[v["payment_id"]].append(v)

    rows = []
    for _, p in batch.iterrows():
        pid = p["payment_id"]
        flags = by_payment.get(pid, [])
        types = [f["violation_type"] for f in flags]
        status = status_for_types(types)
        rows.append({
            "payment_id": pid,
            "vendor_id": p.get("vendor_id"),
            "vendor_name": p.get("vendor_name"),
            "invoice_number": p.get("invoice_number"),
            "amount": float(p["amount"]) if pd.notna(p.get("amount")) else None,
            "status": status,
            "flags": "; ".join(
                f"{VIOLATION_LABELS.get(f['violation_type'], f['violation_type'])}"
                for f in flags
            ),
            "flag_types": ",".join(types),
            "reasons": " | ".join(f["reason"] for f in flags),
        })
    return pd.DataFrame(rows)


def score_batch(batch: pd.DataFrame, violations: List[dict],
                status_df: pd.DataFrame) -> dict:
    total_value = float(pd.to_numeric(batch["amount"], errors="coerce").sum())

    hold_ids = set(status_df.loc[status_df["status"] == STATUS_HOLD, "payment_id"])
    review_ids = set(
        status_df.loc[status_df["status"] == STATUS_REVIEW, "payment_id"]
    )
    flagged_ids = hold_ids | review_ids

    def _sum_for(ids):
        if not ids:
            return 0.0
        return float(pd.to_numeric(
            batch.loc[batch["payment_id"].isin(ids), "amount"], errors="coerce"
        ).sum())

    hold_value = _sum_for(hold_ids)
    flagged_value = _sum_for(flagged_ids)

    red_flags = sum(1 for v in violations if v["severity"] == SEVERITY_RED)
    yellow_flags = sum(
        1 for v in violations
        if v["severity"] == SEVERITY_YELLOW
        and v["violation_type"] not in INFO_VIOLATIONS
    )

    # Value-weighted integrity score: share of batch value that is NOT on hold.
    if total_value > 0:
        integrity_score = round((total_value - hold_value) / total_value * 100, 1)
    else:
        integrity_score = 100.0

    if hold_ids:
        decision = "HOLD - controller review required before release"
    elif review_ids:
        decision = "REVIEW - clear items may proceed, flagged items need sign-off"
    else:
        decision = "CLEAR - batch is ready for authorisation"

    counts_by_type: Dict[str, dict] = {}
    for v in violations:
        entry = counts_by_type.setdefault(
            v["violation_type"], {"count": 0, "value": 0.0}
        )
        entry["count"] += 1
        if v.get("amount"):
            entry["value"] += float(v["amount"])

    return {
        "payment_count": int(len(batch)),
        "total_value": round(total_value, 2),
        "clear_count": int((status_df["status"] == STATUS_CLEAR).sum()),
        "review_count": int((status_df["status"] == STATUS_REVIEW).sum()),
        "hold_count": int((status_df["status"] == STATUS_HOLD).sum()),
        "flagged_count": int(len(flagged_ids)),
        "flagged_value": round(flagged_value, 2),
        "hold_value": round(hold_value, 2),
        "red_flags": red_flags,
        "yellow_flags": yellow_flags,
        "integrity_score": integrity_score,
        "decision": decision,
        "counts_by_type": counts_by_type,
    }
