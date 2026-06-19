"""StageProbe registry: each probe wraps a REAL deep-research graph LLM-call seam.

A probe imports the codebase's actual schema / tools / prompt -- it never reimplements
them -- and exposes a uniform interface the runner drives against any model:

    apply(base_model)  -> runnable   # .with_structured_output(Schema) / .bind_tools(...) / identity
    messages()         -> list       # a small, fixed, checked-in input fixture
    is_valid(response) -> bool       # did the model satisfy this stage's contract?

Contracts:
    tool             - response must carry >=1 tool_call naming a bound tool
    structured       - .with_structured_output(Schema) returns a schema-valid object
    structured_text  - plain text whose parse_lean_facts(...) yields >=1 record
    prose            - non-empty free text (quality scored separately by the judge)
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from open_deep_research import prompts
from open_deep_research.factbase import profile as fbprofile
from open_deep_research.factbase.lean_extract import parse_lean_facts
from open_deep_research.factbase.prompting import build_extraction_prompt
from open_deep_research.state import (
    ConductResearch,
    KnowledgeAssessment,
    ResearchComplete,
    ResearchQuestion,
    TargetProperties,
)
from open_deep_research.utils import tavily_search, think_tool

CONTRACT_TOOL = "tool"
CONTRACT_STRUCTURED = "structured"
CONTRACT_STRUCTURED_TEXT = "structured_text"
CONTRACT_PROSE = "prose"

_DATE = "June 19, 2026"


class _SafeDict(dict):
    """str.format_map helper: leave unknown {placeholders} untouched instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(template: str, **kw: Any) -> str:
    """Format a prompt template, tolerating missing/extra placeholders (prompts drift)."""
    return string.Formatter().vformat(template, (), _SafeDict(**kw))


def _content(resp: Any) -> str:
    c = getattr(resp, "content", resp)
    if isinstance(c, list):  # some providers return content as a list of parts
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c if isinstance(c, str) else str(c)


def _tool_calls_ok(resp: Any) -> bool:
    calls = getattr(resp, "tool_calls", None)
    return bool(calls)


# -- fixtures (small, realistic, fixed so runs are comparable) ----------------
_BRIEF_QUERY = "Review India's digital identity system."
_RESEARCH_TOPIC = "India's foundational digital ID scheme and its population coverage."
_SOURCE_TEXT = (
    "India's Aadhaar is the country's foundational digital identity scheme, operated by "
    "the Unique Identification Authority of India (UIDAI). As of 2024 it had been issued "
    "to over 1.3 billion residents, covering more than 90% of the population. Enrolment "
    "captures biometrics: ten fingerprints, two iris scans and a facial photograph. The "
    "scheme's legal basis is the Aadhaar Act 2016."
)
_FINDINGS = (
    "Aadhaar is India's foundational ID (UIDAI). 1.3B+ enrolments, ~90% coverage. "
    "Biometric capture: fingerprints, iris, face. Legal basis: Aadhaar Act 2016."
)


def _load_profile():
    """The factbase profile used by the property-scoping + extraction probes.

    Prefers the country digital-identity profile; falls back to the first shipped profile so
    the probe still builds if that file is renamed.
    """
    try:
        return fbprofile.load("country_digital_identity")
    except Exception:  # noqa: BLE001 - fall back to whatever ships
        names = [p["name"] for p in fbprofile.available_profiles()]
        if not names:
            raise
        return fbprofile.load(names[0])


@dataclass
class StageProbe:
    """One graph LLM-call seam, drivable against any model. See module docstring."""

    name: str
    contract: str
    apply: Callable[[Any], Any]
    _messages: Callable[[], list]
    is_valid: Callable[[Any], bool]
    max_tokens: int = 2048
    # For prose stages: the instruction text handed to the judge for grounding context.
    judge_context: Callable[[], str] = field(default=lambda: "")

    def messages(self) -> list:
        return self._messages()


def _build_stages() -> list[StageProbe]:
    prof = _load_profile()
    prop_names = [p.name for p in prof.properties]

    # -- tool-calling -------------------------------------------------------
    supervisor = StageProbe(
        name="supervisor",
        contract=CONTRACT_TOOL,
        apply=lambda m: m.bind_tools([ConductResearch, ResearchComplete, think_tool]),
        _messages=lambda: [
            SystemMessage(content=_fmt(
                prompts.lead_researcher_prompt, date=_DATE,
                max_concurrent_research_units=1, max_researcher_iterations=3)),
            HumanMessage(content=f"Research request: {_RESEARCH_TOPIC} Delegate the work."),
        ],
        is_valid=_tool_calls_ok,
    )
    researcher = StageProbe(
        name="researcher",
        contract=CONTRACT_TOOL,
        apply=lambda m: m.bind_tools([tavily_search, think_tool]),
        _messages=lambda: [
            SystemMessage(content=_fmt(prompts.research_system_prompt, mcp_prompt="", date=_DATE)),
            HumanMessage(content=f"Find: {_RESEARCH_TOPIC} Use your tools."),
        ],
        is_valid=_tool_calls_ok,
    )

    # -- structured output --------------------------------------------------
    research_brief = StageProbe(
        name="research_brief",
        contract=CONTRACT_STRUCTURED,
        apply=lambda m: m.with_structured_output(ResearchQuestion),
        _messages=lambda: [HumanMessage(content=_fmt(
            prompts.transform_messages_into_research_topic_prompt,
            messages=f"User: {_BRIEF_QUERY}", date=_DATE))],
        is_valid=lambda r: isinstance(r, ResearchQuestion) and bool(r.research_brief),
    )
    assess_knowledge = StageProbe(
        name="assess_knowledge",
        contract=CONTRACT_STRUCTURED,
        apply=lambda m: m.with_structured_output(KnowledgeAssessment),
        _messages=lambda: [HumanMessage(content=(
            "You assess whether stored knowledge already answers a question.\n"
            f"Stored knowledge: {_FINDINGS}\n"
            "Question: What is the legal basis and population coverage of India's "
            "foundational digital ID?\n"
            "Decide if it is fully answerable and list any missing information."))],
        is_valid=lambda r: isinstance(r, KnowledgeAssessment) and isinstance(r.is_answerable, bool),
    )
    target_properties = StageProbe(
        name="target_properties",
        contract=CONTRACT_STRUCTURED,
        apply=lambda m: m.with_structured_output(TargetProperties),
        _messages=lambda: [HumanMessage(content=(
            "Given the available profile property names below, return the subset needed to "
            "answer the question. Use names EXACTLY as listed.\n"
            f"Available: {', '.join(prop_names)}\n"
            "Question: What is India's ID coverage percentage and its legal basis?"))],
        is_valid=lambda r: isinstance(r, TargetProperties) and isinstance(r.property_names, list),
    )
    extract_facts = StageProbe(
        name="extract_facts",
        contract=CONTRACT_STRUCTURED_TEXT,
        apply=lambda m: m,  # plain text invoke -- no structured-output scaffolding
        _messages=lambda: [HumanMessage(content=build_extraction_prompt(
            prof, None, _SOURCE_TEXT, compiled=False))],
        is_valid=lambda r: len(parse_lean_facts(_content(r))) > 0,
        max_tokens=4096,
    )

    # -- prose --------------------------------------------------------------
    summarize = StageProbe(
        name="summarize",
        contract=CONTRACT_PROSE,
        apply=lambda m: m,
        _messages=lambda: [HumanMessage(content=_fmt(
            prompts.summarize_webpage_prompt, webpage_content=_SOURCE_TEXT, date=_DATE))],
        is_valid=lambda r: bool(_content(r).strip()),
        judge_context=lambda: f"Summarize this source faithfully:\n{_SOURCE_TEXT}",
    )
    compress = StageProbe(
        name="compress",
        contract=CONTRACT_PROSE,
        apply=lambda m: m,
        _messages=lambda: [
            SystemMessage(content=_fmt(prompts.compress_research_system_prompt, date=_DATE)),
            HumanMessage(content=f"Raw research findings:\n{_SOURCE_TEXT}\n\n"
                                 f"{prompts.compress_research_simple_human_message}"),
        ],
        is_valid=lambda r: bool(_content(r).strip()),
        judge_context=lambda: f"Clean up / preserve these findings:\n{_SOURCE_TEXT}",
    )
    final_report = StageProbe(
        name="final_report",
        contract=CONTRACT_PROSE,
        apply=lambda m: m,
        _messages=lambda: [HumanMessage(content=_fmt(
            prompts.final_report_generation_prompt,
            research_brief=_BRIEF_QUERY, findings=_FINDINGS,
            messages=f"User: {_BRIEF_QUERY}", date=_DATE))],
        is_valid=lambda r: bool(_content(r).strip()),
        judge_context=lambda: f"Write a report for '{_BRIEF_QUERY}' grounded in:\n{_FINDINGS}",
        max_tokens=4096,
    )

    return [supervisor, researcher, research_brief, assess_knowledge,
            target_properties, extract_facts, summarize, compress, final_report]


# Built once at import. Probes are pure config; building is cheap (loads one profile).
STAGES: list[StageProbe] = _build_stages()
STAGES_BY_NAME: dict[str, StageProbe] = {p.name: p for p in STAGES}


def content_of(resp: Any) -> str:
    """Public helper: extract text content from a model response (used by the runner)."""
    return _content(resp)
