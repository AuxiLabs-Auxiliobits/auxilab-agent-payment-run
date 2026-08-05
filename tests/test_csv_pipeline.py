"""End-to-end tests that run real synthetic CSV files through the full
pipeline (ingestion -> reference data -> rules -> scoring), exactly the way
``run.py`` does for a real batch.

This complements ``test_rules.py`` (which unit-tests the rule engine
directly against in-memory DataFrames). These tests instead write actual
CSV files to disk and drive them through ``prepay.pipeline.run_validation``,
so the CSV parsing / column-aliasing / reference-data-loading code paths get
exercised too, not just the rules.

Dates are generated relative to "today" (rather than hard-coded) because
``run_validation`` does not accept a ``today`` override -- it always
resolves duplicate/overdue windows against the real current date. Hard-coded
past dates would silently drift out of the relevant windows and the tests
would stop testing anything.

Run with pytest (``pytest -q``).
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from prepay.pipeline import run_validation
from prepay.schema import STATUS_CLEAR, STATUS_HOLD, STATUS_REVIEW

TODAY = pd.Timestamp.now().normalize()


def _write_csv(df: pd.DataFrame, path) -> None:
    df.to_csv(path, index=False)


def _vendor_master(tmp_path):
    return pd.DataFrame([
        {"vendor_id": "V001", "vendor_name": "Acme Ltd",
         "gl_account_code": "ACC-1001", "preferred_payment_method": "ACH",
         "payment_terms": "NET30", "is_active": "True"},
        {"vendor_id": "V002", "vendor_name": "Globex Inc",
         "gl_account_code": "ACC-1002", "preferred_payment_method": "WIRE",
         "payment_terms": "NET45", "is_active": "True"},
    ])


def _empty_invoice_register():
    return pd.DataFrame(
        columns=["payment_id", "invoice_number", "approved_invoice_amount"]
    )


def _empty_payment_history():
    return pd.DataFrame(
        columns=["invoice_number", "vendor_id", "vendor_name", "amount",
                 "payment_date", "status"]
    )


def _build_reference_dir(tmp_path, history=None, invoices=None):
    ref_dir = tmp_path / "reference_data"
    ref_dir.mkdir()
    _write_csv(_vendor_master(tmp_path), ref_dir / "vendor_master.csv")
    _write_csv(
        invoices if invoices is not None else _empty_invoice_register(),
        ref_dir / "invoice_register.csv",
    )
    _write_csv(
        history if history is not None else _empty_payment_history(),
        ref_dir / "payment_history.csv",
    )
    return ref_dir


def _batch_row(**overrides):
    row = {
        "payment_id": "PAY-001",
        "vendor_id": "V001",
        "vendor_name": "Acme Ltd",
        "invoice_number": "INV-001",
        "invoice_amount": 1000.0,
        "payment_amount": 1000.0,
        "payment_method": "ACH",
        "approval_status": "Approved",
        "approver_name": "CFO-JANE",
        "payment_date": TODAY.strftime("%Y-%m-%d"),
        "due_date": (TODAY + timedelta(days=30)).strftime("%Y-%m-%d"),
        "early_pay_discount": "",
        "early_pay_deadline": "",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# 1. Clean batch: everything approved, amounts match, vendor known,
#    no duplicates -> zero flags.
# --------------------------------------------------------------------------
def test_clean_batch_from_csv_has_zero_flags(tmp_path):
    batch = pd.DataFrame([
        _batch_row(payment_id="PAY-001", invoice_number="INV-001"),
        _batch_row(payment_id="PAY-002", vendor_id="V002",
                   vendor_name="Globex Inc", invoice_number="INV-002",
                   payment_method="WIRE"),
    ])
    batch_csv = tmp_path / "payment_batch.csv"
    _write_csv(batch, batch_csv)

    ref_dir = _build_reference_dir(tmp_path)

    result = run_validation(str(batch_csv), reference_dir=str(ref_dir),
                             provider="template")

    assert result.violations == []
    assert set(result.status_df["status"]) == {STATUS_CLEAR}
    assert result.score["hold_count"] == 0
    assert result.score["review_count"] == 0
    assert result.score["clear_count"] == 2


# --------------------------------------------------------------------------
# 2. Duplicate payment: same vendor + same amount, paid within the last
#    30 days according to payment history -> flagged and held.
# --------------------------------------------------------------------------
def test_duplicate_payment_from_csv_is_flagged_and_held(tmp_path):
    batch = pd.DataFrame([
        _batch_row(payment_id="PAY-010", invoice_number="INV-NEW",
                   invoice_amount=2500.0, payment_amount=2500.0),
    ])
    batch_csv = tmp_path / "payment_batch.csv"
    _write_csv(batch, batch_csv)

    history = pd.DataFrame([
        {"invoice_number": "INV-OLD", "vendor_id": "V001",
         "vendor_name": "Acme Ltd", "amount": 2500.0,
         "payment_date": (TODAY - timedelta(days=10)).strftime("%Y-%m-%d"),
         "status": "PAID"},
    ])
    ref_dir = _build_reference_dir(tmp_path, history=history)

    result = run_validation(str(batch_csv), reference_dir=str(ref_dir),
                             provider="template")

    types = {v["violation_type"] for v in result.violations}
    assert "DUPLICATE_PAYMENT" in types

    row = result.status_df.loc[result.status_df["payment_id"] == "PAY-010"].iloc[0]
    assert row["status"] == STATUS_HOLD
    assert result.score["hold_count"] == 1


# --------------------------------------------------------------------------
# 3. Unapproved vendor: payment made to a vendor_id not present in the
#    vendor master -> flagged as unapproved/invalid vendor.
# --------------------------------------------------------------------------
def test_unapproved_vendor_from_csv_is_flagged(tmp_path):
    batch = pd.DataFrame([
        _batch_row(payment_id="PAY-020", vendor_id="V999",
                   vendor_name="Ghost LLC", invoice_number="INV-020"),
    ])
    batch_csv = tmp_path / "payment_batch.csv"
    _write_csv(batch, batch_csv)

    ref_dir = _build_reference_dir(tmp_path)

    result = run_validation(str(batch_csv), reference_dir=str(ref_dir),
                             provider="template")

    types = {v["violation_type"] for v in result.violations}
    assert "INVALID_VENDOR" in types

    row = result.status_df.loc[result.status_df["payment_id"] == "PAY-020"].iloc[0]
    assert row["status"] in (STATUS_HOLD, STATUS_REVIEW)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    fns = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        passed += 1
        print(f"PASS  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")
