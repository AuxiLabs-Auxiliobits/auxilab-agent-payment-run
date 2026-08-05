"""Step 6 (part) - the CFO narrative generator.

The provider is configurable via the ``LLM_PROVIDER`` environment variable:

    template   -> fully offline, deterministic (default; no credentials needed)
    anthropic  -> Anthropic Claude   (needs ANTHROPIC_API_KEY)
    groq       -> Groq-hosted LLaMA  (needs GROQ_API_KEY)
    openai     -> OpenAI             (needs OPENAI_API_KEY)

Regardless of provider, the model only ever receives the *redacted aggregate*
metadata (see ``prepay.redaction``) - never transaction-level identifiers.
If a remote provider fails and LLM_FALLBACK_TO_TEMPLATE is on (default), the
deterministic template is used so the tool never hard-fails on a missing key.
"""
from __future__ import annotations

import json
from typing import Optional

from . import config
from .redaction import assert_redacted


def _fmt_money(value: float) -> str:
    return f"{config.CURRENCY_SYMBOL}{value:,.0f}"


def _system_prompt() -> str:
    return (
        "You are a financial controller assistant. You are given ONLY aggregate, "
        "de-identified metadata about a proposed payment batch - never any "
        "transaction-level identifiers. Write a single, tight paragraph (4-6 "
        "sentences) summarising the batch's readiness for a CFO to authorise. "
        "State the batch value, the integrity score, the overall decision, the "
        "main risk categories by count, and any early-payment discount "
        "opportunities. Be precise and factual; do not invent vendor names, "
        "invoice numbers or payment ids. Do not use markdown headings."
    )


def _user_prompt(metadata: dict) -> str:
    return (
        "Produce the controller narrative from this aggregate metadata as JSON:\n"
        + json.dumps(metadata, indent=2)
    )


def generate_cfo_narrative(metadata: dict, provider: Optional[str] = None) -> str:
    """Return a one-paragraph CFO narrative from redacted aggregate metadata.

    ``provider`` may be a single provider ("groq") or an ordered, comma-separated
    fallback chain ("anthropic,groq"). Providers are tried left to right; the
    first one that succeeds wins. The offline template is always the final
    safety net when LLM_FALLBACK_TO_TEMPLATE is enabled (or when "template"
    appears explicitly in the chain).
    """
    # Hard safety gate: never let identifiers reach a model.
    assert_redacted(metadata)

    # Resolve the ordered provider chain.
    if provider:
        chain = [p.strip().lower() for p in provider.split(",") if p.strip()]
    else:
        chain = list(config.LLM_PROVIDERS) or ["template"]

    dispatch = {
        "anthropic": _anthropic_narrative,
        "groq": _groq_narrative,
        "openai": _openai_narrative,
    }

    errors = []
    for name in chain:
        # "template" ends the chain: use the deterministic offline narrative.
        if name == "template":
            return _template_narrative(metadata)
        fn = dispatch.get(name)
        if fn is None:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            return fn(metadata)
        except Exception as exc:  # pragma: no cover - network/credential dependent
            errors.append(f"{name}: {type(exc).__name__}")

    # Every configured LLM provider failed (or none were usable).
    if config.LLM_FALLBACK_TO_TEMPLATE:
        note = "; ".join(errors) if errors else "no provider configured"
        return (
            _template_narrative(metadata)
            + f"\n\n[note: LLM provider(s) unavailable ({note}); "
            f"used offline template.]"
        )
    raise RuntimeError(
        "All configured LLM providers failed: " + "; ".join(errors)
    )


# --------------------------------------------------------------------------
# Deterministic offline template (default)
# --------------------------------------------------------------------------

def _template_narrative(m: dict) -> str:
    parts = []
    parts.append(
        f"This payment run contains {m['payment_count']} payments totalling "
        f"{_fmt_money(m['total_batch_value'])}, of which "
        f"{_fmt_money(m['flagged_value'])} across {m['flagged_count']} payments "
        f"has been flagged for attention."
    )
    parts.append(
        f"The batch integrity score is {m['integrity_score']}/100 and the "
        f"recommended decision is {m['decision']}."
    )

    risk_bits = []
    if m.get("unapproved_count"):
        risk_bits.append(f"{m['unapproved_count']} unapproved")
    if m.get("duplicate_count"):
        risk_bits.append(f"{m['duplicate_count']} duplicate")
    if m.get("invalid_vendor_count"):
        risk_bits.append(f"{m['invalid_vendor_count']} vendor-master")
    if m.get("amount_mismatch_count"):
        risk_bits.append(f"{m['amount_mismatch_count']} amount-mismatch")
    if m.get("unregistered_invoice_count"):
        risk_bits.append(f"{m['unregistered_invoice_count']} unregistered-invoice")
    if m.get("bank_routing_mismatch_count"):
        risk_bits.append(f"{m['bank_routing_mismatch_count']} bank-routing")
    if m.get("overdue_count"):
        risk_bits.append(f"{m['overdue_count']} overdue")

    if risk_bits:
        parts.append(
            "Key issues requiring resolution before release: "
            + ", ".join(risk_bits)
            + f", with {_fmt_money(m['hold_value'])} held pending controller "
            f"review."
        )
    else:
        parts.append(
            "No blocking control failures were detected in this batch."
        )

    if m.get("discount_missed_count") or m.get("discount_available_count"):
        parts.append(
            f"Early-payment discounts: {m.get('discount_available_count', 0)} "
            f"still available and {m.get('discount_missed_count', 0)} already "
            f"missed - a working-capital opportunity worth monitoring."
        )

    parts.append(
        f"{m['clear_count']} payments are clear to pay; "
        f"{m['review_count']} require review and {m['hold_count']} are on hold."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Remote providers (all receive only redacted metadata)
# --------------------------------------------------------------------------

def _anthropic_narrative(m: dict) -> str:
    import anthropic  # lazy import

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_TIMEOUT
    )
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        system=_system_prompt(),
        messages=[{"role": "user", "content": _user_prompt(m)}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    ).strip()


def _groq_narrative(m: dict) -> str:
    from groq import Groq  # lazy import

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    client = Groq(api_key=config.GROQ_API_KEY, timeout=config.LLM_TIMEOUT)
    resp = client.chat.completions.create(
        model=config.GROQ_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(m)},
        ],
    )
    return resp.choices[0].message.content.strip()


def _openai_narrative(m: dict) -> str:
    from openai import OpenAI  # lazy import

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=config.LLM_TIMEOUT)
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(m)},
        ],
    )
    return resp.choices[0].message.content.strip()
