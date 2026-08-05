"""Canonical batch schema, column aliases and shared constants.

The agent accepts a variety of real-world column names and normalises them to
a single canonical schema so the rule engine can stay simple and vectorised.

The violation taxonomy below maps 1:1 onto the six-step brief:

  Step 2  UNAPPROVED_PAYMENT
  Step 3  DUPLICATE_PAYMENT
  Step 4  OVERDUE_PAYMENT / MISSED_EARLY_PAYMENT_DISCOUNT /
          EARLY_PAYMENT_DISCOUNT_AVAILABLE
  Step 5  INVALID_VENDOR / AMOUNT_MISMATCH / PAYMENT_METHOD_MISMATCH
"""
from __future__ import annotations

# Canonical batch columns used internally by the rule engine.
CANONICAL_COLUMNS = [
    "payment_id",
    "vendor_id",
    "vendor_name",
    "invoice_number",
    "invoice_amount",   # the approved invoice amount (source of truth in-batch)
    "amount",           # the amount that will actually be paid (payment_amount)
    "payment_method",
    "payment_date",
    "invoice_due_date",
    "approval_status",
    "authorizer",       # approver_name
    "early_pay_discount",
    "early_pay_deadline",
]

# Minimum columns a batch must provide to be processable.
REQUIRED_COLUMNS = ["payment_id", "vendor_id", "invoice_number", "amount"]

# Incoming column name -> canonical column name.
COLUMN_ALIASES = {
    "payment_amount": "amount",
    "payment_amt": "amount",
    "pay_amount": "amount",
    "approved_invoice_amount": "invoice_amount",
    "invoice_amt": "invoice_amount",
    "approver_name": "authorizer",
    "approver": "authorizer",
    "approved_by": "authorizer",
    "approval_by": "authorizer",
    "method": "payment_method",
    "pay_method": "payment_method",
    "preferred_method": "payment_method",
    "due_date": "invoice_due_date",
    "invoice_due": "invoice_due_date",
    "vendor": "vendor_name",
    "supplier_id": "vendor_id",
    "supplier_name": "vendor_name",
    "discount_pct": "early_pay_discount",
    "early_payment_discount": "early_pay_discount",
    "discount_deadline": "early_pay_deadline",
    "early_payment_deadline": "early_pay_deadline",
}

# --------------------------------------------------------------------------
# Violation taxonomy
# --------------------------------------------------------------------------
SEVERITY_RED = "RED"
SEVERITY_YELLOW = "YELLOW"
SEVERITY_INFO = "INFO"

# Per-payment status buckets.
STATUS_CLEAR = "Clear"
STATUS_REVIEW = "Review Required"
STATUS_HOLD = "Hold"

# Violation types that force a payment onto HOLD.
BLOCKING_VIOLATIONS = {
    "UNAPPROVED_PAYMENT",   # Step 2
    "DUPLICATE_PAYMENT",    # Step 3
    "INVALID_VENDOR",       # Step 5
    "AMOUNT_MISMATCH",      # Step 5
}

# Violation types that place a payment under REVIEW (but not HOLD).
REVIEW_VIOLATIONS = {
    "OVERDUE_PAYMENT",                 # Step 4
    "MISSED_EARLY_PAYMENT_DISCOUNT",   # Step 4
    "PAYMENT_METHOD_MISMATCH",         # Step 5
}

# Informational only - never changes a payment's status.
INFO_VIOLATIONS = {
    "EARLY_PAYMENT_DISCOUNT_AVAILABLE",  # Step 4
}

# Human-friendly labels for each violation type (used in reports).
VIOLATION_LABELS = {
    "UNAPPROVED_PAYMENT": "Unapproved payment",
    "DUPLICATE_PAYMENT": "Duplicate payment",
    "INVALID_VENDOR": "Vendor not in master",
    "AMOUNT_MISMATCH": "Amount mismatch",
    "OVERDUE_PAYMENT": "Overdue payment",
    "MISSED_EARLY_PAYMENT_DISCOUNT": "Missed early-payment discount",
    "EARLY_PAYMENT_DISCOUNT_AVAILABLE": "Early-payment discount available",
    "PAYMENT_METHOD_MISMATCH": "Payment method mismatch",
}


def status_for_types(violation_types) -> str:
    """Return the worst-case status implied by a set of violation types."""
    types = set(violation_types)
    if types & BLOCKING_VIOLATIONS:
        return STATUS_HOLD
    if types & REVIEW_VIOLATIONS:
        return STATUS_REVIEW
    return STATUS_CLEAR
