"""Tests for _checkpoint_dossier and _facts_report_md (Task 1: partial-dossier-persist)."""
import asyncio
from open_deep_research import deep_researcher as dr


def _setup(monkeypatch, *, fact_count, existing):
    calls = {}
    async def fake_fact_count(db_path, run_id): return fact_count
    async def fake_get_subject(db_path, slug): return existing
    async def fake_report(config, ik): return "## Facts\n- foundational_id_scheme: ID card\n"
    async def fake_save(db_path, *, subject_name, slug, merged_report, sources_union, run, now, run_id):
        calls["save"] = {"subject": subject_name, "status": run.get("status"), "report": merged_report, "run_id": run_id}
        return (1, run_id or 7)
    monkeypatch.setattr(dr, "_run_fact_count", fake_fact_count)
    monkeypatch.setattr(dr, "get_subject_by_slug", fake_get_subject)
    monkeypatch.setattr(dr, "_facts_report_md", fake_report)
    monkeypatch.setattr(dr, "save_run_and_upsert_subject", fake_save)
    return calls

_STATE = {"subject": "Estonia", "prealloc_run_id": 7, "research_brief": "b", "raw_notes": []}
_CFG = {"configurable": {"thread_id": "t", "database_path": "/tmp/x.db"}}

def test_checkpoint_persists_partial_when_facts_and_new_subject(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing=None)
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert calls["save"]["subject"] == "Estonia"
    assert calls["save"]["status"] == "partial"
    assert "ID card" in calls["save"]["report"]
    assert calls["save"]["run_id"] == 7      # idempotent on the preallocated run

def test_checkpoint_skips_when_no_facts(monkeypatch):
    calls = _setup(monkeypatch, fact_count=0, existing=None)
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert "save" not in calls            # Guard 1

def test_checkpoint_skips_existing_dossier(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing={"current_report": "established dossier"})
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert "save" not in calls            # Guard 2

def test_checkpoint_skips_when_no_subject(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing=None)
    asyncio.run(dr._checkpoint_dossier({"prealloc_run_id": 7}, _CFG))
    assert "save" not in calls            # no subject -> no LLM resolution
