"""Central configuration for the Pre-Payment Validation Agent.

Everything tunable lives here so runs are deterministic and reproducible.
All values can be overridden with environment variables (see .env.example).
"""
from __future__ import annotations

import os
from pathlib import Path

# Load a local .env if python-dotenv is installed (optional dependency).
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

REFERENCE_DIR = Path(
    os.getenv("PREPAY_REFERENCE_DIR", str(PROJECT_ROOT / "reference_data"))
)
OUTPUT_DIR = Path(os.getenv("PREPAY_OUTPUT_DIR", str(PROJECT_ROOT / "outputs")))
DATA_DIR = PROJECT_ROOT / "data"

VENDOR_MASTER_CSV = REFERENCE_DIR / "vendor_master.csv"
INVOICE_REGISTER_CSV = REFERENCE_DIR / "invoice_register.csv"
PAYMENT_HISTORY_CSV = REFERENCE_DIR / "payment_history.csv"

# --------------------------------------------------------------------------
# Validation thresholds
# --------------------------------------------------------------------------
# Payment amount may differ from the approved invoice amount by at most this %.
AMOUNT_TOLERANCE_PCT = float(os.getenv("PREPAY_AMOUNT_TOLERANCE_PCT", "0.5"))

# Duplicate: same vendor + same amount paid within this many days.
DUPLICATE_AMOUNT_WINDOW_DAYS = int(os.getenv("PREPAY_DUP_WINDOW_DAYS", "30"))

# Duplicate history is only considered for the last N days.
DUPLICATE_HISTORY_LOOKBACK_DAYS = int(
    os.getenv("PREPAY_HISTORY_LOOKBACK_DAYS", "90")
)

# Duplicate risk scores (0-100) assigned per duplicate match type.
# An exact vendor + invoice-number reuse is a near-certain double payment; a
# vendor + amount coincidence inside the 30-day window is a strong signal but
# slightly more likely to be legitimate (e.g. a recurring fixed charge).
DUPLICATE_RISK_INVOICE = float(os.getenv("PREPAY_DUP_RISK_INVOICE", "95"))
DUPLICATE_RISK_AMOUNT = float(os.getenv("PREPAY_DUP_RISK_AMOUNT", "70"))

# Default early-payment discount policy used when the batch does not carry an
# explicit ``early_pay_discount`` / ``early_pay_deadline`` per row.
EARLY_PAY_DISCOUNT_DEFAULT_PCT = float(os.getenv("PREPAY_DISCOUNT_PCT", "2.0"))
EARLY_PAY_DISCOUNT_WINDOW_DAYS = int(os.getenv("PREPAY_DISCOUNT_DAYS", "10"))

# Map textual payment terms to a number of days.
TERMS_DAYS = {
    "NET7": 7,
    "NET10": 10,
    "NET15": 15,
    "NET30": 30,
    "NET45": 45,
    "NET60": 60,
    "NET90": 90,
}

CURRENCY = os.getenv("PREPAY_CURRENCY", "USD")
CURRENCY_SYMBOL = os.getenv("PREPAY_CURRENCY_SYMBOL", "$")

# --------------------------------------------------------------------------
# LLM narrative provider
# --------------------------------------------------------------------------
# Provider for the CFO narrative. One of: template | anthropic | groq | openai
# "template" is fully offline & deterministic and is the safe default so the
# tool runs with zero credentials.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "template").strip().lower()
# Ordered provider chain parsed from LLM_PROVIDER. Supports a single provider
# ("groq") or a comma-separated priority list ("anthropic,groq"). Providers are
# tried left-to-right; the offline template is the final safety net when
# LLM_FALLBACK_TO_TEMPLATE is on.
LLM_PROVIDERS = [p.strip().lower() for p in LLM_PROVIDER.split(",") if p.strip()]
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "30"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "600"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# If the configured provider fails, silently fall back to the offline template.
LLM_FALLBACK_TO_TEMPLATE = _get_bool("LLM_FALLBACK_TO_TEMPLATE", True)
