"""Tests for the KB-first gate in assess_knowledge (Task 5)."""
import asyncio

import open_deep_research.factbase.completeness as fbc
import open_deep_research.factbase.entities as ent
import open_deep_research.factbase.profile as fbprofile
import open_deep_research.factbase.query as q
import open_deep_research.nodes.brief as brief
from open_deep_research.factbase.profile import Profile, PropertyDef


def _make_profile(props):
    """Build a minimal Profile with the given property names (all required, no qualifiers)."""
    return Profile(
        entity_type="country",
        properties=[PropertyDef(name=p, value_kind="text") for p in props],
    )


def _wire(monkeypatch, *, grouped, subject="Estonia", ledger=None):
    monkeypatch.setattr(brief, "get_subject_names", _aval([subject]))
    monkeypatch.setattr(brief, "_resolve_subject", _aval(subject))
    monkeypatch.setattr(brief, "get_subject_by_slug", _aval({"current_report": "old dossier"}))
    monkeypatch.setattr(ent.CountryResolver, "resolve_in_text", lambda self, s: "EST")
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "EST")

    async def fake_grouped(self, key):
        return grouped

    monkeypatch.setattr(q.FactQuery, "show_grouped", fake_grouped)
    monkeypatch.setattr(brief, "resolve_run_target_properties", _aval(["a", "b"]))

    # Whole-profile mode gate also calls fbprofile.load and fbc.assess_property_status.
    # Stub both so tests don't depend on real YAML files and can control the ledger outcome.
    monkeypatch.setattr(fbprofile, "load", lambda name: _make_profile(["a", "b"]))

    if ledger is not None:
        _fixed_ledger = dict(ledger)

        def fake_assess(grouped_rows, absent, prof):
            return dict(_fixed_ledger)

        monkeypatch.setattr(fbc, "assess_property_status", fake_assess)
    else:
        # Default: all properties resolved (both a and b pass completeness).
        def fake_assess_resolved(grouped_rows, absent, prof):
            return {p.name: "resolved" for p in prof.properties}

        monkeypatch.setattr(fbc, "assess_property_status", fake_assess_resolved)


def _aval(v):
    async def f(*a, **k):
        return v

    return f


def _cfg(**kw):
    base = {
        "use_knowledge_base": True,
        "kb_first_gate": True,
        "whole_profile_mode": True,
        "facts_first_mode": False,
        "allow_clarification": False,
        "kb_reuse_max_age_days": 180,
        "database_path": "/tmp/kbgate.db",
    }
    base.update(kw)
    return {"configurable": base}


def _good(name):
    return {"property_name": name, "in_conflict": False, "trusted_captured_at": "2026-06-01T00:00:00Z"}


def _bad(name):
    return {"property_name": name, "in_conflict": False, "trusted_captured_at": None}


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
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg(kb_first_gate=False,
                                              whole_profile_mode=False, facts_first_mode=False)))
    assert cmd.goto in ("answer_from_dossier", "write_research_brief", "clarify_with_user")


def test_kb_read_error_falls_through(monkeypatch):
    _wire(monkeypatch, grouped=[])

    async def boom(self, key):
        raise RuntimeError("db locked")

    monkeypatch.setattr(q.FactQuery, "show_grouped", boom)
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "write_research_brief"   # falls through to normal research


def test_whole_profile_missing_qualifier_not_reused(monkeypatch):
    """Whole-profile mode: a property with a trusted+recent value but missing_qualifier status
    in the completeness ledger must NOT be marked reusable — it must be researched.

    Here property "a" has a trusted+recent grouped row (is_property_reusable → True) but the
    ledger returns missing_qualifier (a required qualifier is absent).  The gate must route to
    write_research_brief with "a" in target_properties (not to answer_from_facts).
    """
    # "a" is trusted+recent (is_property_reusable would return True for it alone),
    # "b" is also trusted+recent.  But the ledger says "a" has missing_qualifier.
    _wire(
        monkeypatch,
        grouped=[_good("a"), _good("b")],
        ledger={"a": "missing_qualifier", "b": "resolved"},
    )
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    # "a" must NOT be reused despite being trusted+recent — qualifier is missing.
    assert cmd.goto == "write_research_brief"
    assert "a" in cmd.update.get("target_properties", [])
    # "b" IS resolved so it IS reusable and should be skipped.
    assert "b" not in cmd.update.get("target_properties", [])
