"""Codex-suited graph roles use the high-reasoning model slots.

These are dependency-free wiring tests. They do not invoke the Codex CLI; instead
they record the model configuration passed to the LangChain-compatible model seam.
"""
import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

import open_deep_research.deep_researcher as dr
from open_deep_research.nodes import profiles
from open_deep_research.configuration import Configuration
from open_deep_research.state import KnowledgeAssessment, ResearchQuestion, TargetProperties


class _RecordingModel:
    def __init__(self, result):
        self.result = result
        self.configs = []
        self.structured_schemas = []
        self.bound_tools = []

    def with_structured_output(self, schema, *args, **kwargs):
        self.structured_schemas.append(schema)
        return self

    def bind_tools(self, tools, *args, **kwargs):
        self.bound_tools.append(tools)
        return self

    def with_retry(self, *args, **kwargs):
        return self

    def with_config(self, config):
        self.configs.append(config)
        return self

    async def ainvoke(self, *args, **kwargs):
        return self.result


def _config(**overrides):
    configurable = {
        "supervisor_model": "codex:gpt-5.5",
        "final_report_model": "codex:gpt-5.5",
        "summarization_model": "gemini:gemini-2.0-flash",
        "researcher_model": "gemini:gemini-2.0-flash",
        "use_knowledge_base": True,
        "allow_clarification": False,
        "max_structured_output_retries": 1,
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def test_codex_role_config_puts_codex_on_supervisor_and_final_report_only():
    c = Configuration.from_runnable_config(_config())

    assert c.supervisor_model.startswith("codex:")
    assert c.final_report_model.startswith("codex:")
    assert not c.researcher_model.startswith("codex:")
    assert not c.summarization_model.startswith("codex:")


def test_write_research_brief_uses_codex_supervisor_model(monkeypatch):
    model = _RecordingModel(ResearchQuestion(research_brief="Research India DPI."))
    monkeypatch.setattr(dr, "configurable_model", model)
    monkeypatch.setattr(dr, "get_subject_names", lambda *a, **k: [])

    async def no_existing(*args, **kwargs):
        return None

    monkeypatch.setattr(dr, "get_subject_by_slug", no_existing)

    state = {"messages": [HumanMessage(content="Review India DPI")]}
    result = asyncio.run(dr.write_research_brief(state, _config(use_knowledge_base=False)))

    assert result["research_brief"] == "Research India DPI."
    assert ResearchQuestion in model.structured_schemas
    assert model.configs[-1]["model"] == "codex:gpt-5.5"


def test_supervisor_uses_codex_supervisor_model(monkeypatch):
    response = AIMessage(
        content="",
        tool_calls=[{
            "name": "ConductResearch",
            "args": {"research_topic": "India Aadhaar coverage"},
            "id": "call_1",
            "type": "tool_call",
        }],
    )
    model = _RecordingModel(response)
    monkeypatch.setattr(dr, "configurable_model", model)

    state = {
        "supervisor_messages": [HumanMessage(content="Research India DPI")],
        "research_iterations": 0,
    }
    cmd = asyncio.run(dr.supervisor(state, _config()))

    assert cmd.goto == "supervisor_tools"
    assert model.configs[-1]["model"] == "codex:gpt-5.5"
    assert model.bound_tools, "supervisor must bind planning/delegation tools"


def test_assess_knowledge_uses_codex_for_answerability_decision(monkeypatch):
    dossier = {"current_report": "India has Aadhaar.", "updated_at": "2026-06-16"}
    model = _RecordingModel(KnowledgeAssessment(is_answerable=True, missing_information=""))

    async def names(*args, **kwargs):
        return ["India"]

    async def resolve(*args, **kwargs):
        return "India"

    async def by_slug(*args, **kwargs):
        return dossier

    monkeypatch.setattr(dr, "configurable_model", model)
    monkeypatch.setattr(dr, "get_subject_names", names)
    monkeypatch.setattr(dr, "_resolve_subject", resolve)
    monkeypatch.setattr(dr, "get_subject_by_slug", by_slug)

    state = {"messages": [HumanMessage(content="Does India have foundational ID?")]}
    cmd = asyncio.run(dr.assess_knowledge(state, _config()))

    assert cmd.goto == "answer_from_dossier"
    assert KnowledgeAssessment in model.structured_schemas
    assert model.configs[-1]["model"] == "codex:gpt-5.5"


def test_final_report_generation_uses_codex_final_report_model(monkeypatch):
    model = _RecordingModel(AIMessage(content="Final synthesized report"))
    monkeypatch.setattr(dr, "configurable_model", model)

    state = {
        "messages": [HumanMessage(content="Review India DPI")],
        "research_brief": "Review India DPI",
        "notes": ["Aadhaar is foundational ID."],
    }
    result = asyncio.run(dr.final_report_generation(state, _config()))

    assert result["final_report"] == "Final synthesized report"
    assert model.configs[-1]["model"] == "codex:gpt-5.5"


def test_merge_dossier_uses_codex_final_report_model(monkeypatch):
    model = _RecordingModel(AIMessage(content="Merged dossier"))
    monkeypatch.setattr(dr, "configurable_model", model)

    merged = asyncio.run(
        dr._merge_dossier(
            "India",
            "Existing Aadhaar dossier",
            "New MOSIP comparison",
            Configuration.from_runnable_config(_config()),
            _config(),
        )
    )

    assert merged == "Merged dossier"
    assert model.configs[-1]["model"] == "codex:gpt-5.5"


def test_target_property_scoping_stays_on_fast_summarization_model(monkeypatch):
    """A deliberately non-Codex role: cheap structured scoping should stay fast."""
    model = _RecordingModel(TargetProperties(property_names=["id_coverage_pct"]))
    monkeypatch.setattr(profiles, "configurable_model", model)
    prof = SimpleNamespace(properties=[
        SimpleNamespace(name="id_coverage_pct", value_kind="number"),
        SimpleNamespace(name="legal_basis", value_kind="enum"),
    ])

    out = asyncio.run(
        dr.resolve_target_properties(
            "What is India's ID coverage?",
            prof,
            Configuration.from_runnable_config(_config()),
            _config(),
        )
    )

    assert out == ["id_coverage_pct"]
    assert TargetProperties in model.structured_schemas
    assert model.configs[-1]["model"] == "gemini:gemini-2.0-flash"
