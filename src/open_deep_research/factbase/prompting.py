"""Render a domain profile into fact-extraction prompt text (pure, testable).

`compile_property_catalog` turns the selected properties into a human-readable
catalog (name, kind, description, enum vocabulary, qualifiers); `build_extraction_prompt`
wraps it (or, when compiled=False, the legacy names-only form) with the extraction
guardrails. Kept out of deep_researcher.py so it can be unit-tested without the graph.
"""
from __future__ import annotations

_SOURCE_CAP = 24000


def compile_property_catalog(prof, target_properties=None) -> str:
    names = target_properties or [pd.name for pd in prof.properties]
    lines = []
    for name in names:
        try:
            pd = prof.property(name)
        except KeyError:
            continue
        multi = getattr(pd, "multi", False)
        open_world = getattr(pd, "open_world", False)
        kind_label = pd.value_kind
        if pd.value_kind == "enum" and (multi or open_world):
            hints = []
            if multi:
                hints.append("select all that apply")
            if open_world:
                hints.append(
                    "list others verbatim if outside this set" if multi
                    else "use a listed value or give the literal if none fit"
                )
            kind_label = "enum, " + "; ".join(hints)
        line = f"- {pd.name} ({kind_label})"
        if getattr(pd, "description", ""):
            line += f": {pd.description}"
        if pd.value_enum:
            label = "known values" if open_world else "allowed values"
            descs = getattr(pd, "value_enum_descriptions", None) or {}
            if descs:
                vals = ", ".join(f"{v} ({descs[v]})" if v in descs else v for v in pd.value_enum)
                line += f" | {label}: [{vals}]"
            else:
                line += f" | {label}: {pd.value_enum}"
        if pd.qualifier_enums:
            quals = "; ".join(f"{k}={v}" for k, v in pd.qualifier_enums.items())
            line += f" | qualifiers: {quals}"
        elif pd.identity_qualifiers:
            line += f" | qualifiers: {pd.identity_qualifiers}"
        if getattr(pd, "narrative_required", False) and getattr(pd, "narrative_guidance", ""):
            line += f" | narrative (required): {pd.narrative_guidance}"
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
            "for enum properties use the listed values; when a property says 'select all "
            "that apply', return every applicable value separated by commas; when it allows "
            "literals, you may give a value outside the list; for 'text' properties give the "
            "relevant prose verbatim or lightly condensed; "
            "evidence_span MUST be a verbatim substring of the source text supporting the value; "
            "narrative is a short (1-3 sentence) prose note of context the source gives around "
            "the value (caveats, scope, methodology) -- omit it if the source adds nothing; "
            "if nothing is stated, return an empty list.\n"
            "Output: return a JSON array (no prose, no markdown fences). Each element is an "
            "object with keys: property, instance_name, value, evidence_span, and optionally "
            "narrative. For qualifiers, include a 'qualifiers' key whose value is a flat LIST "
            "of the applicable qualifier enum tokens from the catalog above (e.g. "
            "[\"total_pop\", \"issued\"]) -- do NOT emit qualifiers as a nested object, and "
            "include only tokens the source explicitly supports. evidence_span MUST be a "
            "verbatim substring of the source. If nothing is stated, return [].\n"
            "\nSOURCE:\n" + src
        )
    prop_names = target_properties or [pd.name for pd in prof.properties]
    return (
        f"Extract facts about a {entity} from the source text below. "
        f"Only use these property names: {prop_names}. "
        "Emit a qualifier ONLY if the source explicitly states it; otherwise omit it (do not guess). "
        "evidence_span MUST be a verbatim substring of the source text supporting the value. "
        "narrative is an optional short (1-3 sentence) prose note of context around the value. "
        "If nothing is stated, return an empty list.\n"
        "Output: return a JSON array (no prose, no markdown fences). Each element is an "
        "object with keys: property, instance_name, value, evidence_span, and optionally "
        "narrative. For qualifiers, include a 'qualifiers' key whose value is a flat LIST "
        "of the applicable qualifier enum tokens from the catalog above (e.g. "
        "[\"total_pop\", \"issued\"]) -- do NOT emit qualifiers as a nested object, and "
        "include only tokens the source explicitly supports. evidence_span MUST be a "
        "verbatim substring of the source. If nothing is stated, return [].\n"
        "\nSOURCE:\n" + src
    )
