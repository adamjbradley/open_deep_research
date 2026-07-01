"""Profile selection and subject-resolution node helpers."""

import logging

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.prompts import (
    profile_selection_prompt,
    subject_resolution_prompt,
    target_properties_prompt,
)
from open_deep_research.state import (
    SelectedProfile,
    SubjectResolution,
    TargetProperties,
)
from open_deep_research.utils import get_api_key_for_model

logger = logging.getLogger(__name__)

# Configurable model shared by all profile helpers (same default as deep_researcher.py).
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


async def _resolve_subject(topic, research_brief, existing_subjects, configurable, config):
    """Use a cheap model to map this query to a canonical subject name.

    Reuses an existing subject's name verbatim when the query concerns it (even a
    different aspect), so accumulation groups related runs together.
    """
    model = (
        configurable_model
        .with_structured_output(SubjectResolution)
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
    existing = "\n".join(f"- {s}" for s in existing_subjects) or "(none yet)"
    prompt = subject_resolution_prompt.format(
        topic=topic, research_brief=research_brief or topic, existing_subjects=existing
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return response.subject.strip()


def _effective_profile_name(state, configurable) -> str:
    """The profile this run should use: the query-selected one, else the configured default."""
    return (state.get("selected_profile_name") if state else None) or configurable.profile_name


async def select_profile(question, configurable, config) -> str:
    """Pick the profile whose domain best fits the question (query-driven selection).

    A cheap structured call over the shipped profiles; the result is validated against the
    available names and unknowns are dropped. Falls back to ``configurable.profile_name`` on
    any failure, an empty answer, or when only one profile exists -- selection never starves
    the fact path.
    """
    from open_deep_research.factbase import profile as _fbprofile
    try:
        available = _fbprofile.available_profiles()
    except Exception as e:  # noqa: BLE001 - never block research on profile discovery
        logger.warning("available_profiles failed; using configured profile: %s", e)
        return configurable.profile_name
    names = {p["name"] for p in available}
    if len(available) <= 1:
        return configurable.profile_name
    try:
        model = (
            configurable_model
            .with_structured_output(SelectedProfile)
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
        listing = "\n".join(
            f"- {p['name']} (entity: {p['entity_type']}): {p['notes'] or 'no description'} "
            f"| properties: {', '.join(p['property_names'])}"
            for p in available
        )
        prompt = profile_selection_prompt.format(question=question, profiles=listing)
        response = await model.ainvoke([HumanMessage(content=prompt)])
        chosen = (response.profile_name or "").strip()
        if chosen in names:
            logger.info("Query-selected profile: %s", chosen)
            return chosen
        logger.info("No profile matched the question; using configured default %s", configurable.profile_name)
        return configurable.profile_name
    except Exception as e:
        logger.warning("select_profile failed; using configured profile: %s", e)
        return configurable.profile_name


async def resolve_run_target_properties(question, profile_name, configurable, config) -> list[str]:
    """The run's target properties: whole-profile = all props; facts-first = question-scoped."""
    from open_deep_research.factbase import profile as _fbprofile
    prof = _fbprofile.load(profile_name)
    if configurable.whole_profile_mode:
        return [pd.name for pd in prof.properties]
    return await resolve_target_properties(question, prof, configurable, config)


async def resolve_target_properties(question, prof, configurable, config) -> list[str]:
    """Map a question to the subset of profile properties needed to answer it (facts-first).

    A cheap structured call; names are validated against the profile and unknowns dropped.
    Falls back to ALL property names on any failure or empty/all-invalid result, so the
    fact path is never starved.
    """
    all_names = [pd.name for pd in prof.properties]
    try:
        model = (
            configurable_model
            .with_structured_output(TargetProperties)
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
        listing = "\n".join(f"- {pd.name} ({pd.value_kind})" for pd in prof.properties)
        prompt = target_properties_prompt.format(question=question, properties=listing)
        response = await model.ainvoke([HumanMessage(content=prompt)])
        valid = [n for n in (response.property_names or []) if n in all_names]
        return valid or all_names
    except Exception as e:
        logger.warning("resolve_target_properties failed; using all properties: %s", e)
        return all_names
