"""Tests for the knowledge-base entry flow (`assess_knowledge`) and persistence.

Covers the three knowledge-base scenarios plus the storage side:

1. Know nothing            -> no stored dossier  -> route to fresh research.
2. Ask something stored    -> dossier answers it -> route to answer-from-cache.
3. Known subject, new gap  -> dossier incomplete -> route to gap research (+ store later).
4. Storage                 -> a completed run for a new subject is written to SQLite.

Fast and dependency-free: the DB and LLM seams of ``assess_knowledge`` are stubbed
with monkeypatch, and the persistence test runs against a throwaway SQLite file
(``aiosqlite``, stdlib). No model, CLI, or network calls occur.
"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from open_deep_research import deep_researcher
from open_deep_research.deep_researcher import assess_knowledge, persist_research


class _StubModel:
    """Stands in for ``configurable_model``: the declarative chain returns self and
    ``ainvoke`` yields a preset result (the knowledge assessment)."""

    def __init__(self, result):
        self._result = result

    def with_structured_output(self, *args, **kwargs):
        return self

    def with_retry(self, *args, **kwargs):
        return self

    def with_config(self, *args, **kwargs):
        return self

    async def ainvoke(self, *args, **kwargs):
        return self._result


def _assessment(is_answerable: bool, missing_information: str = ""):
    return SimpleNamespace(is_answerable=is_answerable, missing_information=missing_information)


def _patch_subject_resolution(monkeypatch, *, subject: str, dossier: dict | None):
    """Stub the subject-name lookup, LLM subject resolution, and dossier fetch."""
    async def fake_names(db_path):
        return [subject] if dossier else []

    async def fake_resolve(*args, **kwargs):
        return subject

    async def fake_by_slug(db_path, slug):
        return dossier

    monkeypatch.setattr(deep_researcher, "get_subject_names", fake_names)
    monkeypatch.setattr(deep_researcher, "_resolve_subject", fake_resolve)
    monkeypatch.setattr(deep_researcher, "get_subject_by_slug", fake_by_slug)


# 1. Know nothing -> fresh research.
def test_unknown_subject_routes_to_research(monkeypatch):
    _patch_subject_resolution(monkeypatch, subject="Quokka", dossier=None)

    state = {"messages": [HumanMessage(content="What do you know about quokkas?")]}
    config = {"configurable": {"use_knowledge_base": True, "allow_clarification": False}}

    cmd = asyncio.run(assess_knowledge(state, config))

    assert cmd.goto == "write_research_brief"  # research from scratch
    assert cmd.update["subject"] == "Quokka"


# 2. Ask something already stored -> answer from cache.
def test_stored_answerable_routes_to_cache(monkeypatch):
    dossier = {
        "name": "Quokka",
        "current_report": "Quokkas are small marsupials native to Western Australia.",
        "sources": [],
        "updated_at": "2026-06-12",
    }
    _patch_subject_resolution(monkeypatch, subject="Quokka", dossier=dossier)
    monkeypatch.setattr(deep_researcher, "configurable_model", _StubModel(_assessment(True)))

    state = {"messages": [HumanMessage(content="Where do quokkas live?")]}
    config = {"configurable": {"use_knowledge_base": True}}

    cmd = asyncio.run(assess_knowledge(state, config))

    assert cmd.goto == "answer_from_dossier"  # return the stored knowledge
    assert cmd.update["subject"] == "Quokka"


# 3. Known subject, but the question needs info not in the dossier -> research the gap.
def test_known_subject_gap_routes_to_research(monkeypatch):
    dossier = {
        "name": "Quokka",
        "current_report": "Quokkas are small marsupials native to Western Australia.",
        "sources": [],
        "updated_at": "2026-06-12",
    }
    _patch_subject_resolution(monkeypatch, subject="Quokka", dossier=dossier)
    monkeypatch.setattr(
        deep_researcher,
        "configurable_model",
        _StubModel(_assessment(False, missing_information="lifespan and diet")),
    )

    state = {"messages": [HumanMessage(content="How long do quokkas live and what do they eat?")]}
    config = {"configurable": {"use_knowledge_base": True}}

    cmd = asyncio.run(assess_knowledge(state, config))

    assert cmd.goto == "write_research_brief"  # research the gap, not answer from cache
    assert cmd.update["subject"] == "Quokka"
    assert cmd.update["missing_information"] == "lifespan and diet"


# 4. Storage: a completed run for a brand-new subject is written to SQLite.
def test_persist_stores_new_subject(tmp_path):
    db_path = str(tmp_path / "research.db")
    state = {
        "messages": [HumanMessage(content="What do you know about quokkas?")],
        "research_brief": "Research quokkas",
        "final_report": "# Quokkas\nThey are marsupials. Source: https://example.com/quokka",
        "raw_notes": ["a raw research note citing https://src.example/q"],
        "subject": "Quokka",
    }
    config = {
        "configurable": {
            "persist_results": True,
            "accumulate_by_subject": True,
            "use_knowledge_base": True,
            "database_path": db_path,
        }
    }

    result = asyncio.run(persist_research(state, config))

    assert result["subject"] == "Quokka"
    assert isinstance(result["report_id"], int)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        subject = con.execute("SELECT * FROM subjects WHERE slug = 'quokka'").fetchone()
        assert subject is not None, "new subject should be stored"
        assert subject["current_report"] == state["final_report"]  # dossier == this run's report

        run = con.execute(
            "SELECT * FROM research_runs WHERE id = ?", (result["report_id"],)
        ).fetchone()
        assert run["topic"] == "What do you know about quokkas?"
        assert "src.example" in run["raw_notes"]  # raw_notes persisted (JSON array)

        sources = json.loads(subject["sources"])
        assert any("example.com" in s for s in sources)  # URLs extracted from the report
    finally:
        con.close()


# 3b. Researching an ADDITIONAL topic on an existing subject accumulates into the dossier.
def test_persist_accumulates_additional_topic(tmp_path, monkeypatch):
    db_path = str(tmp_path / "research.db")
    config = {
        "configurable": {
            "persist_results": True,
            "accumulate_by_subject": True,
            "use_knowledge_base": True,
            "database_path": db_path,
        }
    }

    # First run establishes the subject (new subject -> no merge).
    first = {
        "messages": [HumanMessage(content="Where do quokkas live?")],
        "research_brief": "Quokka habitat",
        "final_report": "# Quokka\nHabitat: Western Australia. Source: https://a.example/habitat",
        "raw_notes": ["habitat note https://a.example/habitat"],
        "subject": "Quokka",
    }
    res1 = asyncio.run(persist_research(first, config))

    # Second run researches a DIFFERENT aspect of the same subject. The LLM merge is
    # stubbed with a deterministic concat so the accumulation logic is what's tested.
    async def fake_merge(subject, existing_report, new_report, configurable, config):
        return f"{existing_report}\n\n## Additional research\n{new_report}"

    monkeypatch.setattr(deep_researcher, "_merge_dossier", fake_merge)

    second = {
        "messages": [HumanMessage(content="What do quokkas eat?")],
        "research_brief": "Quokka diet",
        "final_report": "# Quokka diet\nThey eat leaves and grasses. Source: https://b.example/diet",
        "raw_notes": ["diet note https://b.example/diet"],
        "subject": "Quokka",
    }
    res2 = asyncio.run(persist_research(second, config))

    assert res2["subject"] == "Quokka"
    assert res2["report_id"] != res1["report_id"]  # a distinct run was recorded

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        subject = con.execute("SELECT * FROM subjects WHERE slug = 'quokka'").fetchone()
        assert subject["run_count"] == 2  # the second run accumulated, not replaced

        # The merged dossier retains the original AND adds the new topic.
        assert "Habitat: Western Australia" in subject["current_report"]
        assert "They eat leaves and grasses" in subject["current_report"]

        # Sources from both runs are unioned.
        sources = json.loads(subject["sources"])
        assert any("a.example" in s for s in sources)
        assert any("b.example" in s for s in sources)

        # Full history kept: two runs and two timestamped dossier snapshots.
        assert con.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM dossier_versions").fetchone()[0] == 2
    finally:
        con.close()
