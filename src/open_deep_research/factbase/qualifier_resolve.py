# src/open_deep_research/factbase/qualifier_resolve.py
"""Resolve a single missing REQUIRED qualifier from a fact's own evidence span.

Pure + injected `model_call` so it is unit-testable without a live model. The model is
asked for the qualifier value as `stated` (in the source) or `inferred` (strongly implied);
inference is only honored when `allow_inference` is True (i.e. targeted research already ran).
"""
from __future__ import annotations

import json


def _first_json_object(text: str) -> dict | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except Exception:  # noqa: BLE001
                        break
        start = text.find("{", start + 1)
    return None


def _build_prompt(*, value, instance_name, property_name, qualifier, enum, evidence_span,
                  allow_inference) -> str:
    mode = ("if the evidence explicitly states it return {\"value\": <token>, \"basis\": "
            "\"stated\"}; if it strongly implies it return {\"value\": <token>, \"basis\": "
            "\"inferred\"}; if neither, return {\"value\": null}."
            if allow_inference else
            "if the evidence explicitly states it return {\"value\": <token>, \"basis\": "
            "\"stated\"}; otherwise return {\"value\": null} (do not guess).")
    return (
        f"Property '{property_name}' (value '{value}') for {instance_name}.\n"
        f"Evidence: \"{evidence_span}\"\n"
        f"The required qualifier '{qualifier}' must be one of {enum}. {mode}\n"
        "Return only the JSON object."
    )


async def resolve_qualifier(*, value, instance_name, property_name, qualifier, enum,
                            evidence_span, allow_inference, model_call) -> dict | None:
    """Resolve a single missing required qualifier from a fact's evidence span."""
    prompt = _build_prompt(
        value=value, instance_name=instance_name, property_name=property_name,
        qualifier=qualifier, enum=enum, evidence_span=evidence_span,
        allow_inference=allow_inference)
    raw = await model_call(prompt)
    obj = _first_json_object(str(raw or ""))
    if not obj:
        return None
    val = obj.get("value")
    basis = obj.get("basis")
    if not val or val not in enum:
        return None
    if basis not in ("stated", "inferred"):
        return None
    if basis == "inferred" and not allow_inference:
        return None
    return {"value": val, "basis": basis}
