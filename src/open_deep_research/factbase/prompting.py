"""Render a domain profile into fact-extraction prompt text (pure, testable).

`compile_property_catalog` turns the selected properties into a human-readable
catalog (name, kind, description, enum vocabulary, qualifiers); `build_extraction_prompt`
wraps it (or, when compiled=False, the legacy names-only form) with the extraction
guardrails. Kept out of deep_researcher.py so it can be unit-tested without the graph.
"""
from __future__ import annotations

_SOURCE_CAP = 8000


def compile_property_catalog(prof, target_properties=None) -> str:
    names = target_properties or [pd.name for pd in prof.properties]
    lines = []
    for name in names:
        try:
            pd = prof.property(name)
        except KeyError:
            continue
        line = f"- {pd.name} ({pd.value_kind})"
        if getattr(pd, "description", ""):
            line += f": {pd.description}"
        if pd.value_enum:
            descs = getattr(pd, "value_enum_descriptions", None) or {}
            if descs:
                vals = ", ".join(f"{v} ({descs[v]})" if v in descs else v for v in pd.value_enum)
                line += f" | allowed values: [{vals}]"
            else:
                line += f" | allowed values: {pd.value_enum}"
        if pd.qualifier_enums:
            quals = "; ".join(f"{k}={v}" for k, v in pd.qualifier_enums.items())
            line += f" | qualifiers: {quals}"
        elif pd.identity_qualifiers:
            line += f" | qualifiers: {pd.identity_qualifiers}"
        lines.append(line)
    return "\n".join(lines)


def build_extraction_prompt(prof, target_properties, source_text, *, compiled: bool) -> str:
    src = (source_text or "")[:_SOURCE_CAP]
    entity = (prof.entity_type or "entity").upper()
    if compiled:
        catalog = compile_property_catalog(prof, target_properties)
        return (
            f"Extract facts about a {entity} from the source text below. "
            "Use ONLY these properties (name, kind, description, allowed values, qualifiers):\n"
            f"{catalog}\n\n"
            "Rules: emit a qualifier ONLY if the source explicitly states it (do not guess); "
            "for enum properties the value MUST be one of the listed allowed values; "
            "evidence_span MUST be a verbatim substring of the source text supporting the value; "
            "if nothing is stated, return an empty list.\n\nSOURCE:\n" + src
        )
    prop_names = target_properties or [pd.name for pd in prof.properties]
    return (
        f"Extract facts about a {entity} from the source text below. "
        f"Only use these property names: {prop_names}. "
        "Emit a qualifier ONLY if the source explicitly states it; otherwise omit it (do not guess). "
        "evidence_span MUST be a verbatim substring of the source text supporting the value. "
        "If nothing is stated, return an empty list.\n\nSOURCE:\n" + src
    )
