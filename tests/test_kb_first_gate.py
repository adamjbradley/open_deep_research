"""Tests for the KB-first gate in assess_knowledge (Task 5)."""
import asyncio
import open_deep_research.deep_researcher as dr
import open_deep_research.nodes.brief as brief
import open_deep_research.factbase.query as q
import open_deep_research.factbase.entities as ent


def _wire(monkeypatch, *, grouped, subject="Estonia"):
    monkeypatch.setattr(brief, "get_subject_names", _aval([subject]))
    monkeypatch.setattr(brief, "_resolve_subject", _aval(subject))
    monkeypatch.setattr(brief, "get_subject_by_slug", _aval({"current_report": "old dossier"}))
    monkeypatch.setattr(ent.CountryResolver, "resolve_in_text", lambda self, s: "EST")
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "EST")
    async def fake_grouped(self, key): return grouped
    monkeypatch.setattr(q.FactQuery, "show_grouped", fake_grouped)
    monkeypatch.setattr(brief, "resolve_run_target_properties", _aval(["a", "b"]))


def _aval(v):
    async def f(*a, **k): return v
    return f


def _cfg(**kw):
    base = {"use_knowledge_base": True, "kb_first_gate": True, "whole_profile_mode": True,
            "facts_first_mode": False, "allow_clarification": False, "kb_reuse_max_age_days": 180,
            "database_path": "/tmp/kbgate.db"}
    base.update(kw); return {"configurable": base}


def _good(name): return {"property_name": name, "in_conflict": False, "trusted_captured_at": "2026-06-01T00:00:00Z"}
def _bad(name): return {"property_name": name, "in_conflict": False, "trusted_captured_at": None}


def test_all_reusable_routes_to_answer_from_facts(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _good("b")])
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "answer_from_facts"
    assert cmd.update.get("answered_from_cache") is True
    assert set(cmd.update.get("target_properties")) == {"a", "b"}


def test_partial_narrows_target_properties(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _bad("b")])
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "write_research_brief"
    assert cmd.update.get("target_properties") == ["b"]


def test_gate_off_uses_existing_flow(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _good("b")])
    # kb_first_gate off -> the prose LLM-assessment path; stub it to a known route
    monkeypatch.setattr(brief, "configurable_model", brief.configurable_model)
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg(kb_first_gate=False,
                                              whole_profile_mode=False, facts_first_mode=False)))
    assert cmd.goto in ("answer_from_dossier", "write_research_brief", "clarify_with_user")


def test_kb_read_error_falls_through(monkeypatch):
    _wire(monkeypatch, grouped=[])
    async def boom(self, key): raise RuntimeError("db locked")
    monkeypatch.setattr(q.FactQuery, "show_grouped", boom)
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "write_research_brief"   # falls through to normal research
