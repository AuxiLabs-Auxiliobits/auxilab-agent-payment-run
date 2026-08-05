"""Unit tests for the strict, brief-conformant rule engine.

Run with pytest (``pytest -q``) or directly (``python -m tests.test_rules``
from the project root, or ``PYTHONPATH=. python tests/test_rules.py``).
Each test isolates one of the brief's six steps.
"""
from __future__ import annotations

import pandas as pd

from prepay.redaction import assert_redacted, build_narrative_metadata
from prepay.rules import run_rules
from prepay.scoring import build_payment_status, score_batch

TODAY = pd.Timestamp("2026-07-19")


# --------------------------------------------------------------------------
# Reference-data fixtures
# --------------------------------------------------------------------------
def _vendors():
    return pd.DataFrame([
        {"vendor_id": "V001", "vendor_name": "Acme Ltd",
         "preferred_payment_method": "ACH", "payment_terms": "NET30",
         "is_active": "True"},
        {"vendor_id": "V002", "vendor_name": "Globex Inc",
         "preferred_payment_method": "WIRE", "payment_terms": "NET45",
         "is_active": "True"},
    ])


def _history():
    return pd.DataFrame([
        {"invoice_number": "INV-500", "vendor_id": "V001",
         "vendor_name": "Acme Ltd", "amount": 4321.55,
         "payment_date": pd.Timestamp("2026-06-20"), "status": "PAID"},
    ])


def _empty_history():
    return pd.DataFrame(
        columns=["invoice_number", "vendor_id", "vendor_name", "amount",
                 "payment_date", "status"]
    )


def _batch(rows):
    """Build a canonical batch DataFrame with sensible clean defaults."""
    base = {
        "vendor_id": "V001",
        "vendor_name": "Acme Ltd",
        "invoice_number": "INV-001",
        "invoice_amount": 1000.0,
        "amount": 1000.0,
        "payment_method": "ACH",
        "approval_status": "Approved",
        "authorizer": "CFO-JANE",
        "payment_date": pd.Timestamp("2026-07-01"),
        "invoice_due_date": pd.Timestamp("2026-07-31"),
        "early_pay_discount": pd.NA,
        "early_pay_deadline": pd.NaT,
    }
    out = []
    for i, r in enumerate(rows):
        row = dict(base)
        row["payment_id"] = r.pop("payment_id", f"P{i+1}")
        row.update(r)
        out.append(row)
    return pd.DataFrame(out)


def _types(violations, payment_id):
    return {v["violation_type"] for v in violations if v["payment_id"] == payment_id}


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------
def test_clean_payment_has_no_violations():
    batch = _batch([{"payment_id": "P1"}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert _types(v, "P1") == set()


# --------------------------------------------------------------------------
# Step 2 - approval
# --------------------------------------------------------------------------
def test_blank_approver_is_unapproved():
    batch = _batch([{"payment_id": "P1", "authorizer": ""}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "UNAPPROVED_PAYMENT" in _types(v, "P1")


def test_non_approved_status_is_unapproved():
    batch = _batch([{"payment_id": "P1", "approval_status": "Pending"}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "UNAPPROVED_PAYMENT" in _types(v, "P1")


# --------------------------------------------------------------------------
# Step 5 - vendor & amount
# --------------------------------------------------------------------------
def test_vendor_not_in_master():
    batch = _batch([{"payment_id": "P1", "vendor_id": "V999",
                     "vendor_name": "Ghost LLC"}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "INVALID_VENDOR" in _types(v, "P1")


def test_amount_mismatch_beyond_tolerance():
    batch = _batch([{"payment_id": "P1", "invoice_amount": 1000.0,
                     "amount": 1200.0}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "AMOUNT_MISMATCH" in _types(v, "P1")


def test_amount_within_tolerance_is_clean():
    batch = _batch([{"payment_id": "P1", "invoice_amount": 1000.0,
                     "amount": 1004.0}])  # 0.4% <= 0.5%
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "AMOUNT_MISMATCH" not in _types(v, "P1")


def test_payment_method_mismatch():
    batch = _batch([{"payment_id": "P1", "payment_method": "CHECK"}])  # V001 -> ACH
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "PAYMENT_METHOD_MISMATCH" in _types(v, "P1")


def _register(rows):
    return pd.DataFrame(rows)


def test_amount_mismatch_uses_invoice_register():
    # The invoice register is the authority: the batch pays 1200 but the
    # register approves 1000 -> 20% over tolerance -> flagged.
    batch = _batch([{"payment_id": "P1", "invoice_amount": 1200.0,
                     "amount": 1200.0}])
    register = _register([{"payment_id": "P1", "invoice_number": "INV-001",
                           "approved_invoice_amount": 1000.0}])
    v, _ = run_rules(batch, _vendors(), _empty_history(), invoices=register,
                     today=TODAY)
    assert "AMOUNT_MISMATCH" in _types(v, "P1")


def test_invoice_register_takes_precedence_over_batch_amount():
    # Register says 1000 and the payment is 1000 -> clean, even though the
    # batch's own invoice_amount (5000) disagrees. The register wins.
    batch = _batch([{"payment_id": "P1", "invoice_amount": 5000.0,
                     "amount": 1000.0}])
    register = _register([{"payment_id": "P1", "invoice_number": "INV-001",
                           "approved_invoice_amount": 1000.0}])
    v, _ = run_rules(batch, _vendors(), _empty_history(), invoices=register,
                     today=TODAY)
    assert "AMOUNT_MISMATCH" not in _types(v, "P1")


# --------------------------------------------------------------------------
# Step 3 - duplicates + risk score
# --------------------------------------------------------------------------
def test_duplicate_same_vendor_and_invoice():
    batch = _batch([{"payment_id": "P1", "vendor_id": "V001",
                     "invoice_number": "INV-500", "amount": 999.0,
                     "invoice_amount": 999.0}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    assert "DUPLICATE_PAYMENT" in _types(v, "P1")
    dup = next(x for x in v if x["payment_id"] == "P1"
              and x["violation_type"] == "DUPLICATE_PAYMENT")
    assert dup["risk_score"] == 95


def test_duplicate_same_vendor_and_amount_within_window():
    batch = _batch([{"payment_id": "P1", "vendor_id": "V001",
                     "invoice_number": "INV-NEW", "amount": 4321.55,
                     "invoice_amount": 4321.55}])
    v, _ = run_rules(batch, _vendors(), _history(), today=TODAY)
    dup = next(x for x in v if x["payment_id"] == "P1"
              and x["violation_type"] == "DUPLICATE_PAYMENT")
    assert dup["risk_score"] == 70


def test_old_history_outside_90_days_is_not_duplicate():
    hist = pd.DataFrame([
        {"invoice_number": "INV-500", "vendor_id": "V001",
         "vendor_name": "Acme Ltd", "amount": 999.0,
         "payment_date": pd.Timestamp("2026-01-01"), "status": "PAID"},
    ])
    batch = _batch([{"payment_id": "P1", "invoice_number": "INV-500"}])
    v, _ = run_rules(batch, _vendors(), hist, today=TODAY)
    assert "DUPLICATE_PAYMENT" not in _types(v, "P1")


# --------------------------------------------------------------------------
# Step 4 - terms & discounts
# --------------------------------------------------------------------------
def test_overdue_payment():
    batch = _batch([{"payment_id": "P1",
                     "payment_date": pd.Timestamp("2026-08-05"),
                     "invoice_due_date": pd.Timestamp("2026-07-31")}])
    v, _ = run_rules(batch, _vendors(), _empty_history(), today=TODAY)
    assert "OVERDUE_PAYMENT" in _types(v, "P1")


def test_missed_and_available_discount():
    batch = _batch([
        {"payment_id": "MISSED", "early_pay_discount": 2.0,
         "early_pay_deadline": pd.Timestamp("2026-06-01")},
        {"payment_id": "OPEN", "early_pay_discount": 2.0,
         "early_pay_deadline": pd.Timestamp("2026-08-01")},
    ])
    v, _ = run_rules(batch, _vendors(), _empty_history(), today=TODAY)
    assert "MISSED_EARLY_PAYMENT_DISCOUNT" in _types(v, "MISSED")
    assert "EARLY_PAYMENT_DISCOUNT_AVAILABLE" in _types(v, "OPEN")


# --------------------------------------------------------------------------
# Step 6 - scoring, status buckets, redaction
# --------------------------------------------------------------------------
def test_scoring_status_buckets():
    batch = _batch([
        {"payment_id": "CLEAR"},
        {"payment_id": "HOLD", "authorizer": ""},                 # unapproved
        {"payment_id": "REVIEW", "early_pay_discount": 2.0,
         "early_pay_deadline": pd.Timestamp("2026-06-01")},        # missed disc
    ])
    v, _ = run_rules(batch, _vendors(), _empty_history(), today=TODAY)
    status_df = build_payment_status(batch, v)
    score = score_batch(batch, v, status_df)
    assert score["clear_count"] == 1
    assert score["review_count"] == 1
    assert score["hold_count"] == 1
    assert score["payment_count"] == 3


def test_redaction_blocks_identifiers():
    batch = _batch([{"payment_id": "P1", "authorizer": ""}])
    v, _ = run_rules(batch, _vendors(), _empty_history(), today=TODAY)
    status_df = build_payment_status(batch, v)
    score = score_batch(batch, v, status_df)
    metadata = build_narrative_metadata(score, provided_batch_id="batch")
    assert_redacted(metadata)  # must not raise
    assert "payment_id" not in metadata
    assert metadata["unapproved_count"] == 1


if __name__ == "__main__":
    fns = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"PASS  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")
