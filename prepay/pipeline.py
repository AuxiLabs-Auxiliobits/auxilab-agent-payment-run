"""End-to-end orchestration of the six validation steps.

    ingest -> load reference -> run rules -> score -> redact -> narrate -> pack

This is the single public entry point (`run_validation`) used by both the CLI
(`run.py`) and the web app (`app.py`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .ingestion import load_batch
from .narrative import generate_cfo_narrative
from .redaction import assert_redacted, build_narrative_metadata
from .reference_data import load_reference_data
from .report import build_json_pack, build_markdown_pack
from .rules import run_rules
from .scoring import build_payment_status, score_batch


class ValidationResult:
    """Container for everything produced by a validation run."""

    def __init__(self, *, batch_id, score, status_df, violations, metadata,
                 narrative, json_pack, markdown_pack):
        self.batch_id = batch_id
        self.score = score
        self.status_df = status_df
        self.violations = violations
        self.metadata = metadata
        self.narrative = narrative
        self.json_pack = json_pack
        self.markdown_pack = markdown_pack

    def write_outputs(self, output_dir: Union[str, Path]) -> dict:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / "authorisation_pack.json"
        md_path = out / "authorisation_pack.md"
        csv_path = out / "payment_status.csv"

        json_path.write_text(json.dumps(self.json_pack, indent=2, default=str))
        md_path.write_text(self.markdown_pack)
        self.status_df.to_csv(csv_path, index=False)
        return {
            "json": str(json_path),
            "markdown": str(md_path),
            "csv": str(csv_path),
        }


def run_validation(
    batch: Union[str, Path, pd.DataFrame],
    reference_dir: Optional[Union[str, Path]] = None,
    provider: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> ValidationResult:
    """Run all six validation steps and return a ValidationResult."""
    # Step 1 - ingestion
    batch_df = load_batch(batch)

    if batch_id is None:
        if isinstance(batch, (str, Path)):
            batch_id = Path(batch).stem
        else:
            batch_id = "payment-batch"

    # Reference data (source of truth): vendor master + invoice register +
    # 90-day payment history.
    ref = load_reference_data(Path(reference_dir) if reference_dir else None)

    # Steps 2-5 - rule engine
    violations, _merged = run_rules(
        batch_df, ref.vendors, ref.history, ref.invoices
    )

    # Step 6 - scoring, status & authorisation pack
    status_df = build_payment_status(batch_df, violations)
    score = score_batch(batch_df, violations, status_df)

    metadata = build_narrative_metadata(score, provided_batch_id=batch_id)
    assert_redacted(metadata)  # safety gate before any LLM call

    narrative = generate_cfo_narrative(metadata, provider=provider)

    json_pack = build_json_pack(
        score, status_df, violations, narrative, metadata, batch_id=batch_id
    )
    markdown_pack = build_markdown_pack(
        score, status_df, narrative, batch_id=batch_id
    )

    return ValidationResult(
        batch_id=batch_id,
        score=score,
        status_df=status_df,
        violations=violations,
        metadata=metadata,
        narrative=narrative,
        json_pack=json_pack,
        markdown_pack=markdown_pack,
    )
