#!/usr/bin/env python3
"""Pre-Payment Validation Agent - command line interface.

Usage:
    python run.py --batch data/payment_batch.csv
    python run.py --batch mybatch.csv --provider anthropic --out outputs/

One command in, a validation report + CFO narrative out. No web server, no
database service, no login.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prepay import config
from prepay.pipeline import run_validation


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate a proposed payment batch and produce a CFO "
                    "authorisation pack.",
    )
    p.add_argument(
        "--batch", "-b", required=True,
        help="Path to the payment batch CSV or JSON to validate.",
    )
    p.add_argument(
        "--reference-dir", "-r", default=None,
        help="Directory holding vendor_master.csv, invoice_register.csv and "
             "payment_history.csv (default: ./reference_data).",
    )
    p.add_argument(
        "--out", "-o", default=str(config.OUTPUT_DIR),
        help="Directory to write the authorisation pack into (default: ./outputs).",
    )
    p.add_argument(
        "--provider", "-p", default=None,
        help="LLM provider for the narrative: template | anthropic | groq | "
             "openai. Overrides LLM_PROVIDER (default: template / env).",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="Only print the file paths that were written.",
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    batch_path = Path(args.batch)
    if not batch_path.exists():
        print(f"error: batch file not found: {batch_path}", file=sys.stderr)
        return 2

    try:
        result = run_validation(
            batch_path,
            reference_dir=args.reference_dir,
            provider=args.provider,
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    paths = result.write_outputs(args.out)

    if args.quiet:
        for kind, path in paths.items():
            print(f"{kind}: {path}")
        return 0

    s = result.score
    sym = config.CURRENCY_SYMBOL
    print()
    print("=" * 66)
    print(f"  PRE-PAYMENT VALIDATION  -  batch: {result.batch_id}")
    print("=" * 66)
    print(f"  Payments           : {s['payment_count']}")
    print(f"  Total batch value  : {sym}{s['total_value']:,.2f}")
    print(f"  Clear / Review / Hold : {s['clear_count']} / "
          f"{s['review_count']} / {s['hold_count']}")
    print(f"  Flagged value      : {sym}{s['flagged_value']:,.2f}")
    print(f"  Integrity score    : {s['integrity_score']}/100")
    print(f"  Decision           : {s['decision']}")
    print("-" * 66)
    print("  Flags by type:")
    if s["counts_by_type"]:
        for vtype, info in sorted(
            s["counts_by_type"].items(), key=lambda kv: -kv[1]["count"]
        ):
            print(f"    - {vtype:<32} {info['count']}")
    else:
        print("    (none)")
    print("-" * 66)
    print("  CFO narrative:")
    print()
    for line in _wrap(result.narrative, 62):
        print(f"    {line}")
    print("-" * 66)
    print("  Written:")
    for kind, path in paths.items():
        print(f"    {kind:<9} {path}")
    print("=" * 66)
    print()
    return 0


def _wrap(text: str, width: int):
    import textwrap

    out = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, width) or [""])
    return out


if __name__ == "__main__":
    raise SystemExit(main())
