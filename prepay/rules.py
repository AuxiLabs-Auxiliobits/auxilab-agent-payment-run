"""Steps 2-5 - the deterministic, pure-pandas rule engine (strict brief spec).

This is the heart of the agent. It never calls an LLM and never makes per-row
database calls; every rule is a vectorised/iterative pandas operation over the
batch joined to the three reference tables (vendor master, invoice register, and
the 90-day payment history).

The rules implement the six-step brief EXACTLY:

  Step 2  Approval status check
          Flag any payment where approval_status is not 'Approved' OR where
          approver_name is blank.

  Step 3  Duplicate payment check (last 90 days of history)
          Flag any payment where the same vendor + invoice number appears in
          history, or where the same vendor + same amount was paid within 30
          days. Each duplicate is assigned a duplicate risk score.

  Step 4  Payment terms compliance
          Flag overdue payments and missed early-payment discounts; surface
          early-payment discount opportunities still available.

  Step 5  Vendor & amount validation
          Flag any payment to a vendor not in the master, any payment amount
          that differs from the approved invoice amount by > 0.5%, and any
          payment method that does not match the vendor's preferred method.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd

from . import config
from .schema import SEVERITY_INFO, SEVERITY_RED, SEVERITY_YELLOW


def _viol(row, severity: str, vtype: str, reason: str,
          risk_score: Optional[float] = None) -> dict:
    amount = None
    if "amount" in row.index and pd.notna(row.get("amount")):
        amount = float(row["amount"])
    vendor = row.get("vendor_name") if "vendor_name" in row.index else None
    if pd.isna(vendor):
        vendor = None
    return {
        "payment_id": row["payment_id"],
        "vendor_id": row.get("vendor_id") if "vendor_id" in row.index else None,
        "vendor": vendor,
        "invoice_number": row.get("invoice_number"),
        "amount": amount,
        "severity": severity,
        "violation_type": vtype,
        "reason": reason,
        "risk_score": risk_score,
    }


def _is_approved(status) -> bool:
    if status is None or pd.isna(status):
        return False
    return str(status).strip().lower() in (
        "approved", "approve", "authorised", "authorized",
    )


def _blank(val) -> bool:
    return val is None or pd.isna(val) or str(val).strip() == ""


def run_rules(
    batch: pd.DataFrame,
    vendors: pd.DataFrame,
    history: pd.DataFrame,
    invoices: Optional[pd.DataFrame] = None,
    today: Optional[pd.Timestamp] = None,
) -> Tuple[List[dict], pd.DataFrame]:
    """Run every validation rule and return (violations, enriched_batch).

    ``invoices`` is the invoice register (payment_id + approved_invoice_amount),
    the authority for the Step 5 amount check. When it is omitted, the amount
    check falls back to any ``invoice_amount`` carried on the batch itself.
    """
    if today is None:
        today = pd.Timestamp.now().normalize()

    # A reliable "is this vendor in the master?" flag survives the left-merge:
    # matched rows get True, unmatched rows get NaN.
    vendors = vendors.copy()
    vendors["_in_master"] = True
    merged = batch.merge(vendors, on="vendor_id", how="left", suffixes=("", "_vm"))

    # Bring the approved invoice amount from the invoice register (Step 5's
    # authority for the amount check), matched on payment_id.
    if (
        invoices is not None
        and not invoices.empty
        and "payment_id" in invoices.columns
        and "approved_invoice_amount" in invoices.columns
    ):
        reg = invoices[["payment_id", "approved_invoice_amount"]].copy()
        reg["approved_invoice_amount"] = pd.to_numeric(
            reg["approved_invoice_amount"], errors="coerce"
        )
        merged = merged.merge(
            reg, on="payment_id", how="left", suffixes=("", "_reg")
        )

    violations: List[dict] = []
    violations.extend(_approval_rules(merged))              # Step 2
    violations.extend(_vendor_and_amount_rules(merged))     # Step 5
    violations.extend(_duplicate_rules(merged, history, today))  # Step 3
    violations.extend(_terms_and_discount_rules(merged, today))  # Step 4
    return violations, merged


# --------------------------------------------------------------------------
# STEP 2 - APPROVAL STATUS CHECK
# --------------------------------------------------------------------------
def _approval_rules(merged: pd.DataFrame) -> List[dict]:
    out: List[dict] = []
    has_status = "approval_status" in merged.columns
    for _, row in merged.iterrows():
        status = row.get("approval_status") if has_status else None
        approver = row.get("authorizer")
        status_ok = _is_approved(status)
        approver_ok = not _blank(approver)
        if status_ok and approver_ok:
            continue
        if not approver_ok and not status_ok:
            reason = ("Payment has no approver on record and approval_status is "
                      f"'{status}' (not 'Approved').")
        elif not approver_ok:
            reason = "Payment has no approver_name on record."
        else:
            reason = f"Payment approval_status is '{status}', not 'Approved'."
        out.append(_viol(row, SEVERITY_RED, "UNAPPROVED_PAYMENT", reason))
    return out


# --------------------------------------------------------------------------
# STEP 5 - VENDOR & AMOUNT VALIDATION
# --------------------------------------------------------------------------
def _vendor_and_amount_rules(merged: pd.DataFrame) -> List[dict]:
    out: List[dict] = []
    has_reg_amt = "approved_invoice_amount" in merged.columns
    has_invoice_amt = "invoice_amount" in merged.columns
    has_pref_method = "preferred_payment_method" in merged.columns
    has_method = "payment_method" in merged.columns

    for _, row in merged.iterrows():
        # 5a - vendor not in the approved master.
        if pd.isna(row.get("_in_master")):
            out.append(_viol(
                row, SEVERITY_RED, "INVALID_VENDOR",
                f"Vendor {row.get('vendor_id')} is not in the approved vendor "
                f"master."))
            # No master record -> cannot run the master-dependent checks.
            continue

        # 5b - payment amount differs from the approved invoice amount by >0.5%.
        # The approved amount comes from the invoice register (the authority);
        # if the register is unavailable, fall back to any invoice_amount that
        # travels with the batch.
        approved = None
        if has_reg_amt and pd.notna(row.get("approved_invoice_amount")):
            approved = float(row["approved_invoice_amount"])
        elif has_invoice_amt and pd.notna(row.get("invoice_amount")):
            approved = float(row["invoice_amount"])

        if approved is not None and approved != 0 and pd.notna(row.get("amount")):
            paid = float(row["amount"])
            diff_pct = abs(paid - approved) / approved * 100.0
            if diff_pct > config.AMOUNT_TOLERANCE_PCT:
                out.append(_viol(
                    row, SEVERITY_RED, "AMOUNT_MISMATCH",
                    f"Payment amount {paid:,.2f} differs from approved invoice "
                    f"amount {approved:,.2f} by {diff_pct:.2f}% "
                    f"(tolerance {config.AMOUNT_TOLERANCE_PCT}%)."))

        # 5c - payment method does not match the vendor's preferred method.
        if has_pref_method and has_method:
            pref = row.get("preferred_payment_method")
            method = row.get("payment_method")
            if not _blank(pref) and not _blank(method) and \
                    str(method).strip().upper() != str(pref).strip().upper():
                out.append(_viol(
                    row, SEVERITY_YELLOW, "PAYMENT_METHOD_MISMATCH",
                    f"Payment method '{method}' does not match vendor's preferred "
                    f"method '{pref}'."))
    return out


# --------------------------------------------------------------------------
# STEP 3 - DUPLICATE PAYMENT CHECK (last 90 days) + duplicate risk score
# --------------------------------------------------------------------------
def _duplicate_rules(merged: pd.DataFrame, history: pd.DataFrame,
                     today: pd.Timestamp) -> List[dict]:
    out: List[dict] = []

    # Restrict history to the configured look-back window (90 days).
    hist = history.copy()
    if "payment_date" in hist.columns:
        cutoff = today - pd.Timedelta(days=config.DUPLICATE_HISTORY_LOOKBACK_DAYS)
        hist = hist[hist["payment_date"].notna() & (hist["payment_date"] >= cutoff)]

    # No history in the look-back window -> no duplicates are possible.
    if hist.empty:
        return out

    # Ensure the amount column is numeric. An empty or string-typed column has
    # dtype 'object', and calling .round() on it raises TypeError on pandas
    # < 2.1 (e.g. Python 3.9). Coercing here keeps the comparison safe on every
    # supported pandas version.
    if "amount" in hist.columns:
        hist["amount"] = pd.to_numeric(hist["amount"], errors="coerce")

    has_inv = "invoice_number" in hist.columns
    has_vendor = "vendor_id" in hist.columns
    has_amt = "amount" in hist.columns

    for _, row in merged.iterrows():
        inv = str(row.get("invoice_number"))
        vid = str(row.get("vendor_id"))
        flagged = False

        # (a) same vendor + invoice number already appears in history.
        if has_inv and has_vendor:
            match = hist[
                (hist["invoice_number"].astype(str) == inv)
                & (hist["vendor_id"].astype(str) == vid)
            ]
            if not match.empty:
                when = match["payment_date"].max() \
                    if "payment_date" in match.columns else None
                when_txt = when.date().isoformat() if pd.notna(when) \
                    else "the last 90 days"
                out.append(_viol(
                    row, SEVERITY_RED, "DUPLICATE_PAYMENT",
                    f"Invoice {inv} for this vendor was already paid on {when_txt} "
                    f"(duplicate risk {config.DUPLICATE_RISK_INVOICE:.0f}/100).",
                    risk_score=config.DUPLICATE_RISK_INVOICE))
                flagged = True

        # (b) same vendor + same amount paid within the 30-day window.
        if not flagged and has_vendor and has_amt and pd.notna(row.get("amount")):
            window = today - pd.Timedelta(days=config.DUPLICATE_AMOUNT_WINDOW_DAYS)
            same = hist[
                (hist["vendor_id"].astype(str) == vid)
                & (hist["amount"].round(2) == round(float(row["amount"]), 2))
            ]
            if "payment_date" in same.columns:
                same = same[same["payment_date"] >= window]
            if not same.empty:
                out.append(_viol(
                    row, SEVERITY_RED, "DUPLICATE_PAYMENT",
                    f"Same vendor + amount {float(row['amount']):,.2f} was paid "
                    f"within the last {config.DUPLICATE_AMOUNT_WINDOW_DAYS} days "
                    f"(duplicate risk {config.DUPLICATE_RISK_AMOUNT:.0f}/100).",
                    risk_score=config.DUPLICATE_RISK_AMOUNT))
    return out


# --------------------------------------------------------------------------
# STEP 4 - PAYMENT TERMS COMPLIANCE + EARLY-PAYMENT DISCOUNT
# --------------------------------------------------------------------------
def _terms_and_discount_rules(merged: pd.DataFrame,
                              today: pd.Timestamp) -> List[dict]:
    out: List[dict] = []
    for _, row in merged.iterrows():
        pay_date = row.get("payment_date")
        due_date = row.get("invoice_due_date")

        # Overdue: paying after the due date.
        if pd.notna(pay_date) and pd.notna(due_date) and pay_date > due_date:
            out.append(_viol(
                row, SEVERITY_YELLOW, "OVERDUE_PAYMENT",
                f"Payment date {pay_date.date()} is after the invoice due date "
                f"{due_date.date()}."))

        # Early-payment discount handling.
        discount = row.get("early_pay_discount")
        deadline = row.get("early_pay_deadline")
        if pd.notna(discount) and float(discount) > 0 and pd.notna(deadline):
            pct = float(discount)
            amount = row.get("amount")
            saving = (float(amount) * pct / 100.0) if pd.notna(amount) else None
            saving_txt = f" (~{config.CURRENCY_SYMBOL}{saving:,.0f})" if saving \
                else ""
            if deadline >= today:
                out.append(_viol(
                    row, SEVERITY_INFO, "EARLY_PAYMENT_DISCOUNT_AVAILABLE",
                    f"{pct:g}% early-payment discount available until "
                    f"{deadline.date()}{saving_txt}."))
            else:
                out.append(_viol(
                    row, SEVERITY_YELLOW, "MISSED_EARLY_PAYMENT_DISCOUNT",
                    f"Missed {pct:g}% early-payment discount{saving_txt}; deadline "
                    f"was {deadline.date()}."))
    return out
