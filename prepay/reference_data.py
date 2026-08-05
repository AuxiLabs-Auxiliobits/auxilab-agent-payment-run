"""Reference-data access layer (the \"source of truth\").

Loads the three authoritative reference tables named in the brief from CSV
files:

  * vendor_master.csv     - the approved vendor list (50 vendors in the sample)
  * invoice_register.csv  - the approved invoice amount per payment; this is the
                            authority for the Step 5 amount check
                            (payment_id, invoice_number, approved_invoice_amount)
  * payment_history.csv   - recent payment history (120 rows in the sample)

These CSVs *are* the store, which keeps the tool standalone and runnable in
minutes with no database service to set up. Each file backs exactly one of the
brief's controls:

  vendor_master     -> Step 5 vendor validation
  invoice_register  -> Step 5 approved-amount validation
  payment_history   -> Step 3 duplicate detection

Column names are normalised to lower snake-case so downstream rules can rely on
a stable schema regardless of how the source files were exported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config


class ReferenceDataError(FileNotFoundError):
    """Raised when a required reference file is missing or empty."""


@dataclass
class ReferenceData:
    vendors: pd.DataFrame
    invoices: pd.DataFrame
    history: pd.DataFrame


def _read_csv(path: Path, label: str, required: bool = True) -> pd.DataFrame:
    if not path.exists():
        if not required:
            return pd.DataFrame()
        raise ReferenceDataError(
            f"{label} not found at {path}. Set PREPAY_REFERENCE_DIR or place the "
            f"file under reference_data/."
        )
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


def load_reference_data(reference_dir: Optional[Path] = None) -> ReferenceData:
    ref_dir = Path(reference_dir) if reference_dir else config.REFERENCE_DIR

    vendors = _read_csv(ref_dir / "vendor_master.csv", "vendor_master.csv")
    invoices = _read_csv(ref_dir / "invoice_register.csv", "invoice_register.csv")
    history = _read_csv(ref_dir / "payment_history.csv", "payment_history.csv")

    # ---- vendor master ----
    # (is_active is kept for reference/reporting; vendor existence is the
    # brief's Step 5 test, handled in the rule engine.)
    if "is_active" in vendors.columns:
        vendors["is_active_bool"] = ~vendors["is_active"].astype(str).str.strip(
        ).str.lower().isin(("0", "false", "no", "n", "inactive"))

    # ---- invoice register ----
    # The approved invoice amount is the authority for the Step 5 amount check.
    if "approved_invoice_amount" in invoices.columns:
        invoices["approved_invoice_amount"] = pd.to_numeric(
            invoices["approved_invoice_amount"], errors="coerce"
        )

    # ---- payment history ----
    if "amount" in history.columns:
        history["amount"] = pd.to_numeric(history["amount"], errors="coerce")
    if "payment_date" in history.columns:
        history["payment_date"] = pd.to_datetime(
            history["payment_date"], errors="coerce"
        )

    return ReferenceData(vendors=vendors, invoices=invoices, history=history)
