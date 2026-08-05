"""Pre-Payment Validation Agent.

A standalone, deterministic accounts-payable control that validates a proposed
payment batch against the vendor master, invoice register and payment history,
then produces a controller authorisation pack with an LLM-written CFO narrative.

The LLM never validates payments and never sees transaction-level identifiers -
it only converts de-identified aggregate metadata into a narrative.
"""
from __future__ import annotations

__version__ = "1.0.0"

from .pipeline import ValidationResult, run_validation

__all__ = ["run_validation", "ValidationResult", "__version__"]
