"""Step 1 - Batch ingestion.

Accept a proposed payment batch as CSV or JSON, normalise its columns to the
canonical schema and coerce types. No validation logic lives here - this layer
only guarantees a clean, well-typed DataFrame for the rule engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

import pandas as pd

from .schema import COLUMN_ALIASES, REQUIRED_COLUMNS


class BatchIngestionError(ValueError):
    """Raised when a batch cannot be parsed into the canonical schema."""


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    # lower snake-case every incoming header
    df = df.rename(
        columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns}
    )
    # apply canonical aliases
    df = df.rename(columns={c: COLUMN_ALIASES.get(c, c) for c in df.columns})
    # de-duplicate any columns produced by aliasing (keep first)
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def load_batch(source: Union[str, Path, pd.DataFrame]) -> pd.DataFrame:
    """Load and normalise a payment batch.

    ``source`` may be a path to a .csv/.json file or an existing DataFrame.
    """
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        path = Path(source)
        if not path.exists():
            raise BatchIngestionError(f"Batch file not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            df = pd.read_csv(path, dtype=str)
        elif suffix in (".json", ".jsonl"):
            df = pd.read_json(path, lines=(suffix == ".jsonl"), dtype=str)
        else:
            raise BatchIngestionError(
                f"Unsupported batch format '{suffix}'. Use .csv or .json."
            )

    if df.empty:
        raise BatchIngestionError("The uploaded batch contains no rows.")

    df = _normalise_columns(df)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise BatchIngestionError(
            "Batch is missing required column(s): "
            + ", ".join(missing)
            + f". Found columns: {list(df.columns)}"
        )

    df = _coerce_types(df)

    # Stable, unique payment ids are required for reporting.
    if df["payment_id"].duplicated().any():
        dups = df.loc[df["payment_id"].duplicated(), "payment_id"].tolist()
        raise BatchIngestionError(f"Duplicate payment_id values in batch: {dups}")

    return df.reset_index(drop=True)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    # trim whitespace on all string cells
    for col in df.columns:
        df[col] = df[col].astype("string").str.strip()

    # numeric amount fields
    for col in ("amount", "invoice_amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # early-payment discount may arrive as "2%", "1.5 %", "2", or blank/"None";
    # strip the percent sign and whitespace before parsing to a number so a
    # value like "2%" is not silently coerced to NaN.
    if "early_pay_discount" in df.columns:
        df["early_pay_discount"] = pd.to_numeric(
            df["early_pay_discount"].astype("string")
            .str.replace("%", "", regex=False)
            .str.strip(),
            errors="coerce",
        )

    # date fields (kept as datetime64; NaT when unparseable/empty)
    for col in ("payment_date", "invoice_due_date", "early_pay_deadline"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # empty strings -> NA for the optional text columns
    for col in ("authorizer", "approval_status", "bank_routing", "vendor_name",
                "payment_method"):
        if col in df.columns:
            df[col] = df[col].replace({"": pd.NA})

    return df
