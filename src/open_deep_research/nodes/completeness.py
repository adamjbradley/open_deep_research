"""Completeness-assessment nodes: sufficiency, gap-loop, absence judgement."""

import logging

import aiosqlite
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel
from typing import Literal

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.state import AgentState
from open_deep_research.storage import get_db_path
from open_deep_research.utils import get_api_key_for_model
from open_deep_research.nodes.persistence import _checkpoint_dossier
from open_deep_research.nodes.profiles import _effective_profile_name

logger = logging.getLogger(__name__)

# Configurable model shared by completeness helpers (same default as deep_researcher.py).
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


def _target_property_coverage(grouped_rows, target_properties):
    """For each target property, whether the fact base has it (present) and a trusted value."""
    present = {p: False for p in target_properties}
    trusted = {p: False for p in target_properties}
    for r in grouped_rows:
        p = r.get("property_name")
        if p in present:
            present[p] = True
            if r.get("admission") == "trusted" and not r.get("in_conflict"):
                trusted[p] = True
    return present, trusted


async def assess_sufficiency(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "answer_from_facts"]]:
    """Facts-first: are the question's target properties covered? If not, loop to research the gaps.

    Routes back to write_research_brief (a gap round) when target properties are still missing and
    the round budget (max_fact_rounds) allows; otherwise to answer_from_facts.
    """
    configurable = Configuration.from_runnable_config(config)
    targets = state.get("target_properties") or []
    subject = state.get("subject")
    rounds_used = state.get("fact_rounds_used", 0) or 0

    missing = []
    if targets and subject:
        try:
            import aiosqlite
            from open_deep_research.factbase import entities as fbentities, query as fbquery
            instance_key = fbentities.CountryResolver().resolve(subject)
            if instance_key:
                async with aiosqlite.connect(get_db_path(config)) as conn:
                    grouped = await fbquery.FactQuery(conn).show_grouped(instance_key)
                present, _trusted = _target_property_coverage(grouped, targets)
                missing = [p for p in targets if not present[p]]
        except Exception as e:
            logger.warning("assess_sufficiency check failed (treating as still-missing): %s", e)
            missing = list(targets)

    if missing and rounds_used + 1 < configurable.max_fact_rounds:
        logger.info("Facts insufficient (missing %s); gap round %d", missing, rounds_used + 1)
        gap = (
            "The following facts are still missing and MUST be found: "
            + ", ".join(missing) + ". Search specifically for these."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "fact_rounds_used": rounds_used + 1,
                    "target_properties": missing},
        )
    if missing:
        logger.info("Facts still missing %s but round budget exhausted; answering with what we have", missing)
    return Command(goto="answer_from_facts", update={"fact_rounds_used": rounds_used})


def _gaploop_decision(incomplete, prev_incomplete, rounds_used, max_rounds):
    """Pure whole-profile gap-loop routing decision (no I/O).

    Returns ``(goto, no_progress)``:
      - ``goto``: "write_research_brief" for another gap round, else "synthesize_narrative" (finalize).
      - ``no_progress``: True when a gap round (rounds_used >= 1) closed ZERO gaps -- the
        still-incomplete required-property set is unchanged from the prior round. ``incomplete``
        only stays-same or shrinks across rounds, so set-equality is a valid no-progress test.

    Bail-out: the first no-progress gap round finalizes instead of looping (aggressive threshold).
    """
    no_progress = (
        rounds_used >= 1
        and prev_incomplete is not None
        and set(incomplete) == set(prev_incomplete)
    )
    if incomplete and not no_progress and rounds_used + 1 < max_rounds:
        return "write_research_brief", no_progress
    return "synthesize_narrative", no_progress


def _qualifier_gap_directive(missing_rq: dict) -> tuple[str, list[str]]:
    """Build the axis-aware gap directive + the list of "<prop>::<qualifier>" axes it targets."""
    lines, axes = [], []
    for prop, items in missing_rq.items():
        for it in items:
            q, enum = it["qualifier"], it["enum"]
            lines.append(
                f"{prop}: the value is known, but its required '{q}' ({' vs '.join(enum)}) is "
                f"unconfirmed -- find a PRIMARY/official source (statute, act, or regulator) "
                f"stating it.")
            axes.append(f"{prop}::{q}")
    return ("\n".join(lines), axes)


async def assess_completeness(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "synthesize_narrative"]]:
    """Whole-profile: loop until every REQUIRED property is resolved-or-confirmed-absent or budget hit.

    Routes to write_research_brief (gap round) while any required property is incomplete and
    the round budget (max_profile_rounds) allows; otherwise routes to synthesize_narrative (Task 7).
    """
    import aiosqlite
    from open_deep_research.factbase import (
        entities as fbentities,
        query as fbquery,
        profile as fbprofile,
        completeness as fbc,
        schema as fbschema,
        migrations as fbmig,
    )
    from open_deep_research.factbase.property_status import PropertyStatusStore

    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    rounds_used = state.get("fact_rounds_used", 0) or 0
    prof = fbprofile.load(_effective_profile_name(state, configurable))

    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    if not ik:
        # Can't resolve subject to a country — go straight to terminal
        return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})

    # Persist a partial dossier from the facts gathered so far (cheap, no LLM) BEFORE the
    # loop/finalize decision, so a run aborted/timed-out in a later gap round still saved a
    # usable dossier (the empty-dossier-on-timeout failure). Best-effort.
    await _checkpoint_dossier(state, config)

    notes_text = "\n".join(state.get("raw_notes", []) or [])[:8000]

    async with aiosqlite.connect(get_db_path(config)) as conn:
        await fbmig.apply(conn, fbschema.STEPS)
        store = PropertyStatusStore(conn)
        absent = await store.absent_properties(ik)
        grouped = await fbquery.FactQuery(conn).show_grouped(ik)
        ledger = fbc.assess_property_status(grouped, absent, prof)

        # Affirmative-absence pass for still-missing REQUIRED properties (bounded by this round).
        model_call = _make_absence_judge_call(configurable, config)
        for pd in prof.properties:
            if (pd.completeness == "required"
                    and ledger.get(pd.name) == "missing_value"
                    and getattr(pd, "absence_allowed", False)
                    and pd.name not in absent):
                if await judge_absence(pd.name, pd.description, notes_text, model_call):
                    await store.record_absent(
                        ik, pd.name, {}, "no data after targeted search",
                        state.get("prealloc_run_id"), None,
                    )
                    ledger[pd.name] = "confirmed_absent"

        await conn.commit()

    incomplete = [
        pd.name for pd in prof.properties
        if pd.completeness == "required" and not fbc.is_complete(ledger.get(pd.name, "missing_value"), pd)
    ]
    # Prioritize biggest gaps first so a bounded gap round (and its brief) leads with the
    # properties that need the most -- missing_value before missing_qualifier/narrative.
    incomplete = fbc.order_incomplete_by_severity(incomplete, ledger)
    goto, no_progress = _gaploop_decision(
        incomplete, state.get("prev_incomplete_props"), rounds_used, configurable.max_profile_rounds
    )
    if goto == "write_research_brief":
        logger.info("Whole-profile incomplete (%s); gap round %d", incomplete, rounds_used + 1)
        gap = (
            "These profile properties are still incomplete and MUST be resolved or, if no data "
            "exists, explicitly confirmed unavailable after searching: "
            + ", ".join(f"{p} ({ledger.get(p)})" for p in incomplete) + "."
        )
        mrq = fbc.missing_required_qualifiers(grouped, prof)
        qtext, qaxes = _qualifier_gap_directive(mrq)
        if qtext:
            gap = gap + "\n" + qtext
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "target_properties": incomplete,
                    "fact_rounds_used": rounds_used + 1,
                    "prev_incomplete_props": incomplete,
                    "qualifier_research_attempted": sorted(
                        set(state.get("qualifier_research_attempted") or []) | set(qaxes))},
        )
    if no_progress:
        logger.info("Gap round closed zero gaps (%s unchanged); bailing out to finalize", incomplete)
    elif incomplete:
        logger.info("Whole-profile still incomplete %s but round budget exhausted; finishing", incomplete)
    return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})


class AbsenceJudgement(BaseModel):
    """Whether a property is genuinely absent for the subject after a targeted search."""

    absent: bool


async def judge_absence(prop_name, prop_desc, notes_text, model_call) -> bool:
    """True only if the model affirms no data exists for this property after searching.

    Best-effort: any error -> False (treat as still-missing, keep trying within budget).
    """
    try:
        res = await model_call(prop_name, prop_desc, notes_text)
        return bool(getattr(res, "absent", False))
    except Exception as e:  # noqa: BLE001
        logger.warning("absence judge failed (non-fatal) for %s: %s", prop_name, e)
        return False


def _make_absence_judge_call(configurable, config):
    """An async ``model_call(prop_name, prop_desc, notes_text) -> AbsenceJudgement``
    on the cheap summarization chain; returns absent=True only if the notes confirm
    no data exists for this property (not just that it was not covered yet)."""
    async def model_call(prop_name, prop_desc, notes_text):
        model = (
            configurable_model
            .with_structured_output(AbsenceJudgement)
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
        prompt = (
            f"Research notes about a subject are below. For the property '{prop_name}' "
            f"({prop_desc}), did the research look for it and find that NO data exists / it "
            f"is not applicable? Set absent=true ONLY if the notes show a genuine, searched "
            f"absence; set absent=false if it simply wasn't covered yet.\n\nNOTES:\n{notes_text}"
        )
        return await model.ainvoke([HumanMessage(content=prompt)])
    return model_call
