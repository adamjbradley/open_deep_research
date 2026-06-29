"""Integration test: assess_completeness records qualifier_research_attempted handoff.

Verifies that when assess_completeness detects a required qualifier is missing for a
property (missing_qualifier status), it records the axis in the returned Command's
``qualifier_research_attempted`` update, which the resolver node later reads to allow
inference.
"""
import asyncio
import json
import os
import tempfile

import aiosqlite

from open_deep_research.factbase import migrations as fbmig
from open_deep_research.factbase import schema as fbschema
from open_deep_research.nodes import completeness as completeness_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_db(db_path: str, instance_key: str = "EST") -> None:
    """Apply migrations and insert a trusted fact for data_protection_law with NO stage qualifier."""
    async with aiosqlite.connect(db_path) as conn:
        # Migration step 2 does ALTER TABLE research_runs; pre-create it so migrations apply cleanly.
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS research_runs "
            "(id INTEGER PRIMARY KEY, topic TEXT, status TEXT, "
            "coverage_incomplete INTEGER DEFAULT 0, last_heartbeat TEXT, profile_name TEXT, "
            "profile_version TEXT, profile_hash TEXT);"
        )
        await conn.commit()
        await fbmig.apply(conn, fbschema.STEPS)
        # Insert a source so the fact LEFT JOIN resolves (optional but keeps the row realistic).
        await conn.execute(
            "INSERT INTO source (url_or_domain, tier) VALUES (?, ?)",
            ("https://example.com", "official"),
        )
        # Insert a trusted fact: value present, qualifiers_json has NO 'stage'
        # Qualification gap: stage is a required qualifier but absent → missing_qualifier status.
        await conn.execute(
            "INSERT INTO fact (tuple_key, instance_key, property_name, qualifiers_json, "
            "as_of, value, unit, canonical_value, admission, lifecycle, run_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (
                f"{instance_key}|data_protection_law",  # tuple_key (approximate)
                instance_key,
                "data_protection_law",
                json.dumps({}),            # NO stage qualifier → missing_qualifier
                None,
                "true",
                None,
                "true",
                "trusted",
                "current",
                "test-run-1",
            ),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_assess_completeness_records_qualifier_axis_in_handoff(monkeypatch):
    """assess_completeness includes 'data_protection_law::stage' in qualifier_research_attempted.

    Setup: DB seeded with a trusted value for data_protection_law but no 'stage' qualifier.
    Expected: the function routes to write_research_brief and the Command.update includes
    qualifier_research_attempted containing "data_protection_law::stage".
    """
    # Stub _checkpoint_dossier so the test stays offline (no DB subjects table needed).
    async def noop_checkpoint(state, config):
        pass

    monkeypatch.setattr(completeness_mod, "_checkpoint_dossier", noop_checkpoint)

    # Stub judge_absence: should not be called for missing_qualifier, but guard for safety.
    async def no_model_call(*args, **kwargs):
        return False

    monkeypatch.setattr(completeness_mod, "judge_absence", no_model_call)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        asyncio.run(_seed_db(db_path))

        state = {
            "subject": "Estonia",
            "fact_rounds_used": 0,
            "raw_notes": [],
            "research_brief": "b",
            "prev_incomplete_props": None,
            "qualifier_research_attempted": [],
        }
        cfg = {
            "configurable": {
                "thread_id": "t",
                "database_path": db_path,
                "whole_profile_mode": True,
                "profile_name": "country_digital_identity",
            }
        }

        result = asyncio.run(completeness_mod.assess_completeness(state, cfg))

        # The function should decide another gap round is needed (some required properties
        # are incomplete including data_protection_law with missing_qualifier status).
        assert result.goto == "write_research_brief", (
            f"Expected gap round (write_research_brief) but got '{result.goto}'. "
            f"update={result.update}"
        )

        attempted = result.update.get("qualifier_research_attempted") or []
        assert "data_protection_law::stage" in attempted, (
            f"Expected 'data_protection_law::stage' in qualifier_research_attempted, "
            f"got: {attempted}"
        )
    finally:
        os.unlink(db_path)
