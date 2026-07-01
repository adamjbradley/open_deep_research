"""Brief-phase nodes: clarify scope, assess knowledge, and write the research brief."""

import logging
from typing import Literal

import aiosqlite

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, get_buffer_string
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.prompts import (
    answer_from_dossier_prompt,
    clarify_with_user_instructions,
    knowledge_assessment_prompt,
    lead_researcher_prompt,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.state import (
    AgentState,
    ClarifyWithUser,
    KnowledgeAssessment,
    ResearchQuestion,
)
from open_deep_research.storage import (
    get_db_path,
    get_subject_by_slug,
    get_subject_names,
    slugify,
)
from open_deep_research.utils import get_api_key_for_model, get_today_str
from open_deep_research.nodes.profiles import (
    _resolve_subject,
    select_profile,
    resolve_target_properties,
    resolve_run_target_properties,
)

logger = logging.getLogger(__name__)

# Initialize a configurable model that we will use throughout the agent.
# Backed by CLI agents (Gemini, Claude/code, or Codex). Default = gemini:gemini-2.5-flash:
# the standard Gemini CLI is reliable for structured output; Codex's exec sandbox runs
# repo commands (e.g. pytest) so it stays opt-in per role until that's restricted.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


async def clarify_with_user(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "__end__"]]:
    """Analyze user messages and ask clarifying questions if the research scope is unclear.

    This function determines whether the user's request needs clarification before proceeding
    with research. If clarification is disabled or not needed, it proceeds directly to research.

    Args:
        state: Current agent state containing user messages

        config: Runtime configuration with model settings and preferences

    Returns:
        Command to either end with a clarifying question or proceed to research brief
    """
    # Step 1: Check if clarification is enabled in configuration
    configurable = Configuration.from_runnable_config(config)
    if not configurable.allow_clarification:
        # Skip clarification step and proceed directly to research
        return Command(goto="write_research_brief")

    # Step 2: Prepare the model for structured clarification analysis
    messages = state["messages"]
    model_config = {
        "model": configurable.supervisor_model,
        "model_chain": configurable.model_chain("supervisor"),
        "stage": "supervisor",
        "max_tokens": configurable.researcher_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.supervisor_model, config),
        "tags": ["langsmith:nostream"]
    }

    # Configure model with structured output and retry logic
    clarification_model = (
        configurable_model
        .with_structured_output(ClarifyWithUser)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(model_config)
    )

    # Step 3: Analyze whether clarification is needed
    prompt_content = clarify_with_user_instructions.format(
        messages=get_buffer_string(messages),
        date=get_today_str()
    )
    response = await clarification_model.ainvoke([HumanMessage(content=prompt_content)])

    # Step 4: Route based on clarification analysis
    if response.need_clarification:
        # End with clarifying question for user
        return Command(
            goto=END,
            update={"messages": [AIMessage(content=response.question)]}
        )
    else:
        # Proceed to research with verification message
        return Command(
            goto="write_research_brief",
            update={"messages": [AIMessage(content=response.verification)]}
        )


async def assess_knowledge(state: AgentState, config: RunnableConfig) -> Command[Literal["answer_from_dossier", "write_research_brief", "clarify_with_user", "answer_from_facts"]]:
    """Entry node: match the question to a stored subject and decide how to handle it.

    - The dossier already answers the question  -> answer straight from the cache.
    - Partially covered (or a refresh is asked) -> research the gap and merge.
    - A brand-new subject                       -> clarify scope (optional) then research.

    Args:
        state: Current agent state containing the user's messages
        config: Runtime configuration with knowledge-base settings

    Returns:
        Command routing to answer-from-cache, research, or clarification.
    """
    configurable = Configuration.from_runnable_config(config)
    question = get_buffer_string(state.get("messages", []))

    # Knowledge base disabled: preserve the original clarify -> research flow.
    if not configurable.use_knowledge_base:
        return Command(goto="clarify_with_user")

    db_path = get_db_path(config)

    # Step 1: Ensure the subject matches (reuse an existing subject when applicable).
    try:
        existing_names = await get_subject_names(db_path)
        subject = await _resolve_subject(question, question, existing_names, configurable, config)
    except Exception as e:
        logger.warning("Subject match failed in assess_knowledge: %s", e)
        return Command(goto="clarify_with_user")

    existing = await get_subject_by_slug(db_path, slugify(subject))
    dossier = (existing or {}).get("current_report") if existing else None

    # KB-first gate (facts-first / whole-profile): skip already-good properties before round 1.
    if configurable.kb_first_gate and (configurable.facts_first_mode or configurable.whole_profile_mode):
        try:
            from open_deep_research.factbase.reuse import is_property_reusable
            from open_deep_research.factbase.query import FactQuery
            from collections import defaultdict
            from open_deep_research.factbase.entities import CountryResolver
            from datetime import datetime, timezone
            profile_name = configurable.profile_name  # selected profile (config default)
            targets = await resolve_run_target_properties(question, profile_name, configurable, config)
            ik = CountryResolver().resolve_in_text(subject) or CountryResolver().resolve(subject)
            now = datetime.now(timezone.utc)
            reusable = []
            if ik:
                async with aiosqlite.connect(db_path) as _conn:
                    grouped = await FactQuery(_conn).show_grouped(ik)
                by_prop = defaultdict(list)   # a property can have several grouped rows
                for g in grouped:
                    by_prop[g.get("property_name")].append(g)
                reusable = [p for p in targets
                            if by_prop.get(p) and is_property_reusable(
                                by_prop[p], now=now, max_age_days=configurable.kb_reuse_max_age_days)]
            to_research = [p for p in targets if p not in reusable]
            if targets and not to_research:
                return Command(goto="answer_from_facts",
                               update={"subject": subject, "answered_from_cache": True,
                                       "target_properties": reusable})
            if reusable:  # partial: research only the delta
                gap = ("These properties are already known and trusted (skipped): "
                       + ", ".join(reusable) + ". Research only: " + ", ".join(to_research) + ".")
                return Command(goto="write_research_brief",
                               update={"subject": subject, "target_properties": to_research,
                                       "missing_information": gap})
        except Exception as e:
            logger.warning("KB-first gate failed; researching normally: %s", e)
        # nothing reusable (or error) -> fall through to the normal flow below

    # Step 2: New subject -> clarify scope (if enabled) then research.
    if not dossier:
        target = "clarify_with_user" if configurable.allow_clarification else "write_research_brief"
        return Command(goto=target, update={"subject": subject})

    # Step 3: We have prior knowledge -> does it already answer the question?
    is_answerable = False
    missing_information = ""
    try:
        assessment_model = (
            configurable_model
            .with_structured_output(KnowledgeAssessment)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.supervisor_model,
                "model_chain": configurable.model_chain("supervisor"),
                "stage": "supervisor",
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.supervisor_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        assessment = await assessment_model.ainvoke([HumanMessage(content=knowledge_assessment_prompt.format(
            subject=subject, date=get_today_str(), research_brief=question, dossier=dossier,
        ))])
        is_answerable = bool(assessment.is_answerable)
        missing_information = assessment.missing_information or ""
    except Exception as e:
        logger.warning("Knowledge assessment failed, treating as a gap: %s", e)

    if is_answerable:
        # Fully covered: answer directly from the stored dossier.
        return Command(goto="answer_from_dossier", update={"subject": subject})
    # Partial / refresh: research the gap (subject already known, skip clarification).
    return Command(
        goto="write_research_brief",
        update={"subject": subject, "missing_information": missing_information},
    )


async def answer_from_dossier(state: AgentState, config: RunnableConfig) -> dict:
    """Answer the question directly from the subject's stored dossier (no research)."""
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    question = get_buffer_string(state.get("messages", []))
    existing = await get_subject_by_slug(get_db_path(config), slugify(subject)) if subject else None
    dossier = (existing or {}).get("current_report") if existing else ""
    updated_at = (existing or {}).get("updated_at") or get_today_str()

    answer_model = configurable_model.with_config({
        "model": configurable.final_report_model,
        "model_chain": configurable.model_chain("final_report"),
        "stage": "final_report",
        "max_tokens": configurable.final_report_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"],
    })
    response = await answer_model.ainvoke([HumanMessage(content=answer_from_dossier_prompt.format(
        subject=subject, question=question, updated_at=updated_at, dossier=dossier,
    ))])
    answer = str(response.content)
    # Static edge to persist_research (logs the Q&A run; dossier is unchanged).
    return {
        "final_report": answer,
        "messages": [AIMessage(content=answer)],
        "answered_from_cache": True,
        "subject": subject,
    }


def _steer_brief_with_catalog(research_brief: str, prof, target_properties: list) -> str:
    """Augment a facts-first research brief with the profile's compiled property catalog.

    Steering with bare property NAMES under-specifies the research: the researcher isn't told
    a property's definition, allowed values, or required qualifiers, so it gathers loose
    variants and values without the qualifiers extraction needs (e.g. a coverage % with no
    population basis). Injecting ``compile_property_catalog`` -- the same definitions/qualifiers
    used for extraction -- tells the researcher exactly what to find, and to capture qualifiers.
    """
    from open_deep_research.factbase.prompting import compile_property_catalog
    catalog = compile_property_catalog(prof, target_properties)
    return (
        f"{research_brief}\n\nGather the specific facts needed to answer this. For each "
        f"property below, find a cited value that matches its definition and allowed values, "
        f"and capture any listed qualifier the sources state (e.g. the population basis for a "
        f"coverage percentage):\n{catalog}"
    )


async def write_research_brief(state: AgentState, config: RunnableConfig) -> dict:
    """Build the research brief and initialize the supervisor.

    If we already have a dossier for the resolved subject, the brief is gap-scoped
    (research what's missing, verify the rest, include the dossier as context).
    Otherwise it is generated from the user's messages as a fresh research question.

    Args:
        state: Current agent state containing user messages (and resolved subject)
        config: Runtime configuration with model settings

    Returns:
        State update with the research brief and initialized supervisor messages
    """
    configurable = Configuration.from_runnable_config(config)
    question = get_buffer_string(state.get("messages", []))
    subject = state.get("subject")
    missing_information = state.get("missing_information") or ""

    # Query-driven profile selection: pick the best-matching domain profile once per run (a
    # gap round re-enters this node, so reuse the already-selected one). Threaded via state so
    # extract_facts uses the same profile. Falls back to configurable.profile_name.
    selected_profile_name = state.get("selected_profile_name")
    if configurable.auto_select_profile and not selected_profile_name:
        selected_profile_name = await select_profile(question, configurable, config)
    profile_name = selected_profile_name or configurable.profile_name

    # Facts-first answers/sufficiency resolve the fact-base instance from `subject`, but on
    # the research path subject is otherwise only resolved at persist time (and not at all
    # when the KB is off). Resolve it here so the facts nodes have an instance to query.
    if (configurable.facts_first_mode or configurable.whole_profile_mode) and not subject:
        try:
            existing_names = await get_subject_names(get_db_path(config))
            subject = await _resolve_subject(question, question, existing_names, configurable, config)
        except Exception as e:
            logger.warning("facts-first subject resolution failed: %s", e)

    # Load the dossier (if any) for the resolved subject to scope the research.
    dossier = None
    if subject:
        existing = await get_subject_by_slug(get_db_path(config), slugify(subject))
        dossier = (existing or {}).get("current_report") if existing else None

    if dossier and configurable.whole_profile_mode:
        # Whole-profile gap round: the missing properties are the PRIMARY objective; the prior
        # dossier is reference-only. Re-verifying the whole (often large) dossier would crowd out
        # the actual gaps under a bounded research budget, so it is demoted to context.
        research_brief = (
            f"Research the subject \"{subject}\" to FILL SPECIFIC GAPS in an existing dossier.\n\n"
            f"PRIMARY OBJECTIVE -- find cited values for the properties that are currently "
            f"missing: {missing_information or '(complete or refresh any out-of-date facts)'}\n"
            f"Go straight to sources that directly cover them (for a legal or regulatory "
            f"property, the official statute, act, or regulator).\n\n"
            f"The existing dossier below is REFERENCE ONLY -- consult it to avoid redundant work; "
            f"do NOT spend research effort re-verifying or re-gathering what it already covers:\n{dossier}"
        )
    elif dossier:
        # KB refresh of a known subject: verify the existing dossier against current sources
        # and extend it (here re-verification IS the goal, unlike a whole-profile gap round).
        research_brief = (
            f"Research the subject \"{subject}\" to fully answer this question:\n{question}\n\n"
            f"Focus in particular on the information that is currently missing: "
            f"{missing_information or '(complete or refresh any out-of-date facts)'}\n\n"
            f"Verify the existing facts below against current sources and extend them; "
            f"do not merely repeat them:\n{dossier}"
        )
    else:
        # New subject: generate a focused research brief from the user's messages.
        supervisor_model = (
            configurable_model
            .with_structured_output(ResearchQuestion)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.supervisor_model,
                "model_chain": configurable.model_chain("supervisor"),
                "stage": "supervisor",
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.supervisor_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        response = await supervisor_model.ainvoke([HumanMessage(content=transform_messages_into_research_topic_prompt.format(
            messages=question, date=get_today_str()
        ))])
        research_brief = response.research_brief

    # Facts-first / whole-profile: resolve which fact properties to target and steer research.
    target_properties = state.get("target_properties")
    if configurable.facts_first_mode or configurable.whole_profile_mode:
        from open_deep_research.factbase import profile as _fbprofile
        from open_deep_research.nodes.profiles import resolve_run_target_properties
        _prof = _fbprofile.load(profile_name)
        if not target_properties:
            target_properties = await resolve_run_target_properties(
                question, profile_name, configurable, config)
        if target_properties:
            # Steer research with the property catalog (definitions, allowed values,
            # qualifiers) -- not just bare names -- so facts are gathered with their qualifiers.
            research_brief = _steer_brief_with_catalog(research_brief, _prof, target_properties)

    supervisor_system_prompt = lead_researcher_prompt.format(
        date=get_today_str(),
        max_concurrent_research_units=configurable.max_concurrent_research_units,
        max_researcher_iterations=configurable.max_researcher_iterations
    )
    # Routed to research_supervisor (see graph wiring).
    update = {
        "research_brief": research_brief,
        "subject": subject,
        "supervisor_messages": {
            "type": "override",
            "value": [
                SystemMessage(content=supervisor_system_prompt),
                HumanMessage(content=research_brief)
            ]
        }
    }
    if target_properties:
        update["target_properties"] = target_properties
    if selected_profile_name:
        update["selected_profile_name"] = selected_profile_name
    return update
