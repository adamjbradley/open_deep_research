"""Synthesis node helpers: facts-answer rendering, dossier narrative, name consolidation."""

import logging

from langchain_core.messages import AIMessage, HumanMessage, get_buffer_string
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.nodes.profiles import _effective_profile_name
from open_deep_research.prompts import facts_answer_polish_prompt
from open_deep_research.state import AgentState
from open_deep_research.storage import get_db_path
from open_deep_research.utils import get_api_key_for_model

logger = logging.getLogger(__name__)

# Configurable model shared by all synthesis helpers (same default as deep_researcher.py).
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


def _best_singular_row(rows: list) -> dict:
    """Pick the single best row for a singular property: most-corroborated, then prefer a
    non-conflicting value, then the longest (most specific) value. Deterministic.

    Sources name the same scheme many ways ("e-ID" / "electronic identification" / "personal
    ID code"); text canonicalization can't merge generic synonyms (and value_aliases are
    property-global, so aliasing them would corrupt other countries), so a singular property
    collapses to its best value at render time rather than dumping every variant.
    """
    return max(rows, key=lambda r: (
        r.get("source_count", 0),
        1 if r.get("admission") == "trusted" else 0,   # a trusted value beats a provisional one
        0 if r.get("in_conflict") else 1,
        len(str(r.get("value") or "")),
    ))


def _display_value(row: dict) -> str:
    """A readable value for the answer: a raw surface form from ``variants`` (the longest, most
    complete one), not the noise-stripped canonical that ``group_by_canonical`` puts in ``value``
    (e.g. show "Estonia's digital ID", not the canonical "estonia s digital")."""
    variants = [v for v in (row.get("variants") or []) if v and v.strip()]
    if variants:
        return max(variants, key=len)
    return str(row.get("value") or "")


class NameConsolidation(BaseModel):
    """Whether several extracted name-variants denote the SAME entity, and the best name."""

    same_entity: bool
    canonical_name: str = ""


async def _consolidate_name_group(subject, prop_name, prop_desc, rows, model_call):
    """Merge name-variants that denote the same entity into one row, via a best-effort model.

    Deterministic canonicalization can't merge synonyms ("e-ID" / "electronic identification"
    / "Digi-ID" are the same scheme); this asks the model whether the distinct variants name
    the same thing and, if so, returns ONE merged row using the model's canonical name (with
    corroboration summed). Returns None (keep variants as-is) when there is <2 to merge, the
    model says they differ, or anything fails -- so the deterministic path still applies.
    """
    values = []
    for r in rows:
        v = _display_value(r)
        if v and v not in values:
            values.append(v)
    if len(values) < 2:
        return None
    try:
        result = await model_call(subject, prop_name, prop_desc, values)
    except Exception as e:  # noqa: BLE001 - consolidation is best-effort
        logger.warning("name consolidation failed (non-fatal) for %s: %s", prop_name, e)
        return None
    if not (result and getattr(result, "same_entity", False)
            and (getattr(result, "canonical_name", "") or "").strip()):
        return None
    canonical = result.canonical_name.strip()
    best = _best_singular_row(rows)
    return {
        **best,
        "value": canonical,
        "variants": [canonical],
        "source_count": sum(int(r.get("source_count") or 0) for r in rows),
        "in_conflict": False,
        "admission": "trusted" if any(r.get("admission") == "trusted" for r in rows)
        else best.get("admission"),
    }


def _make_name_consolidation_call(configurable, config):
    """An async ``model_call(subject, prop_name, prop_desc, values) -> NameConsolidation``
    on the cheap summarization chain, grounded strictly in the provided values."""
    async def model_call(subject, prop_name, prop_desc, values):
        model = (
            configurable_model
            .with_structured_output(NameConsolidation)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.summarization_model,
                "model_chain": configurable.model_chain("summarization"),
                "stage": "summarization",
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.summarization_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        listing = "; ".join(f'"{v}"' for v in values)
        prompt = (
            f"Different sources gave these values for the '{prop_name}' of {subject}.\n"
            f"Property meaning: {prop_desc}\nValues: {listing}\n\n"
            f"Do these all refer to the SAME {prop_name} (the same scheme/entity), just named "
            f"differently? If yes, set same_entity=true and canonical_name to the single best, "
            f"most official, commonly-used name -- choose from or lightly normalise the given "
            f"values; do NOT invent new information. If they are genuinely different things, "
            f"set same_entity=false."
        )
        return await model.ainvoke([HumanMessage(content=prompt)])
    return model_call


def _facts_answer_text(subject, grouped_rows, targets, singular_props=None) -> str:
    """Deterministic, grounded answer from the grouped fact base: one line per target
    property (value | status | sources); missing targets flagged explicitly.

    ``singular_props`` names properties that hold a single value (non-``multi``); for those,
    multiple source-variants are collapsed to the single best row (see ``_best_singular_row``)
    instead of listing every variant.
    """
    singular = set(singular_props or ())
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    lines = [f"# {subject or 'Subject'} — facts"]
    for p in (targets or sorted(by_prop)):
        rows = by_prop.get(p)
        if not rows:
            lines.append(f"- **{p}**: missing — not found in sources.")
            continue
        if p in singular and len(rows) > 1:
            rows = [_best_singular_row(rows)]
        for r in rows:
            status = "trusted" if (r.get("admission") == "trusted" and not r.get("in_conflict")) else \
                ("in-conflict" if r.get("in_conflict") else "provisional")
            lines.append(f"- **{p}**: {_display_value(r)} ({status}, {r.get('source_count', 0)} sources)")
            narrative = (r.get("narrative") or "").strip()
            if narrative:
                lines.append(f"  - {narrative}")
    return "\n".join(lines)


async def _synthesize_dossier(subject, grouped, absent, overview_sections, model_call) -> str:
    """Profile-defined subject narrative grounded ONLY in gathered facts; deterministic fallback."""
    facts_block = _facts_answer_text(subject, grouped, None)   # readable, raw-value listing
    if not overview_sections:
        return facts_block
    try:
        sections = "\n".join(f"- {s}" for s in overview_sections)
        absent_line = ("Explicitly note these have no data: " + ", ".join(sorted(absent))) if absent else ""
        prompt = (f"Write a concise dossier about {subject}. Cover EACH section below as a '## ' "
                  f"heading, grounded ONLY in the facts provided -- cite nothing not present, and "
                  f"state absences plainly. {absent_line}\n\nSECTIONS:\n{sections}\n\nFACTS:\n{facts_block}")
        resp = await model_call(prompt)
        text = str(getattr(resp, "content", "") or "").strip()
        return text or facts_block
    except Exception as e:  # noqa: BLE001
        logger.warning("narrative synthesis failed; using deterministic facts: %s", e)
        return facts_block


async def synthesize_narrative(state: AgentState, config: RunnableConfig) -> dict:
    """Whole-profile: write a profile-defined subject dossier from gathered facts + confirmed-absent set."""
    import aiosqlite
    from open_deep_research.factbase import (entities as fbentities, query as fbquery,
        profile as fbprofile)
    from open_deep_research.factbase.property_status import PropertyStatusStore
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    prof = fbprofile.load(_effective_profile_name(state, configurable))
    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    grouped, absent = [], set()
    if ik:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(ik)
            absent = await PropertyStatusStore(conn).absent_properties(ik)

    async def mc(prompt):
        model_name = configurable.facts_answer_polish_model or configurable.summarization_model
        model = configurable_model.with_config({
            "model": model_name,
            "model_chain": configurable.model_chain("final_report"),
            "stage": "final_report",
            "max_tokens": configurable.final_report_model_max_tokens,
            "api_key": get_api_key_for_model(model_name, config),
            "tags": ["langsmith:nostream"],
        })
        return await model.ainvoke([HumanMessage(content=prompt)])

    answer = await _synthesize_dossier(
        subject, grouped, absent, getattr(prof, "overview_sections", []), mc)
    return {"final_report": answer, "messages": [AIMessage(content=answer)], "subject": subject}


async def answer_from_facts(state: AgentState, config: RunnableConfig) -> dict:
    """Facts-first: answer the question directly from the structured fact base (no prose report)."""
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    question = get_buffer_string(state.get("messages", []))
    targets = state.get("target_properties") or []

    import aiosqlite
    from open_deep_research.factbase import entities as fbentities, query as fbquery
    # Resolve the country from the subject PHRASE (e.g. "Estonia's digital identity scheme"),
    # not just an exact country name -- extraction stores facts under the country key (EST),
    # so the answer path must find that country inside the descriptive subject or it retrieves
    # nothing and renders every property "missing".
    instance_key = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    grouped = []
    if instance_key:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(instance_key)
    if targets:
        grouped = [r for r in grouped if r.get("property_name") in targets]

    # Singular (non-multi) properties collapse to their single best value at render time.
    singular_props = set()
    prof = None
    try:
        from open_deep_research.factbase import profile as fbprofile
        prof = fbprofile.load(_effective_profile_name(state, configurable))
        singular_props = {pd.name for pd in prof.properties if not getattr(pd, "multi", False)}
    except Exception as e:  # noqa: BLE001 - profile is best-effort here; fall back to listing all
        logger.warning("facts-answer: could not load profile for singular collapse: %s", e)

    # (C) Semantic consolidation: merge name-variants that denote the same entity (e.g.
    # "e-ID"/"electronic identification"/"Digi-ID") into one canonical value via a best-effort
    # LLM pass -- deterministic canonicalization can't merge synonyms. Falls back silently.
    if configurable.consolidate_name_values and prof is not None and subject:
        name_singular = {pd.name for pd in prof.properties
                         if not getattr(pd, "multi", False)
                         and getattr(pd, "value_kind", None) in ("name", "name_year")}
        if targets:
            name_singular &= set(targets)
        if name_singular:
            model_call = _make_name_consolidation_call(configurable, config)
            by_prop = {}
            for r in grouped:
                by_prop.setdefault(r.get("property_name"), []).append(r)
            for p in name_singular:
                rows = by_prop.get(p) or []
                if len(rows) <= 1:
                    continue
                try:
                    desc = getattr(prof.property(p), "description", "") or ""
                except Exception:  # noqa: BLE001
                    desc = ""
                merged = await _consolidate_name_group(subject, p, desc, rows, model_call)
                if merged:
                    grouped = [r for r in grouped if r.get("property_name") != p] + [merged]

    deterministic = _facts_answer_text(subject, grouped, targets, singular_props=singular_props)

    # Optional cheap-LLM polish, grounded ONLY in the deterministic facts (best-effort).
    answer = deterministic
    try:
        polish_model_name = configurable.facts_answer_polish_model or configurable.summarization_model
        polish_model = configurable_model.with_config({
            "model": polish_model_name,
            "model_chain": configurable.model_chain("facts_answer_polish"),
            "stage": "facts_answer_polish",
            "max_tokens": configurable.summarization_model_max_tokens,
            "api_key": get_api_key_for_model(polish_model_name, config),
            "tags": ["langsmith:nostream"],
        })
        resp = await polish_model.ainvoke([HumanMessage(content=facts_answer_polish_prompt.format(
            question=question, facts=deterministic))])
        polished = str(resp.content).strip()
        if polished:
            answer = polished
    except Exception as e:
        logger.warning("facts answer polish failed; using deterministic answer: %s", e)

    return {
        "final_report": answer,
        "messages": [AIMessage(content=answer)],
        "subject": subject,
    }
