"""Facts-first mode: target-property scoping, sufficiency loop, answer-from-facts.

Dependency-free: routing/render/coverage are pure; assess_sufficiency hits a temp
SQLite fact base (no LLM); the target-property LLM call is faked via a stub model.
"""
import asyncio

import aiosqlite
from langgraph.graph import END

import open_deep_research.deep_researcher as dr
from open_deep_research.factbase import migrations, profile, schema
from open_deep_research.state import TargetProperties

DI = profile.load("country_digital_identity")


def _cfg(**kw):
    return {"configurable": kw}


# -- routing functions (pure, config-driven) -----------------------------------

def test_route_after_research_respects_facts_first(monkeypatch):
    # Isolate from an ambient FACTS_FIRST_MODE in the developer's .env: env overrides the
    # per-call configurable, which would otherwise mask the facts_first_mode=False branch.
    monkeypatch.delenv("FACTS_FIRST_MODE", raising=False)
    assert dr.route_after_research({}, _cfg(facts_first_mode=True)) == "extract_facts"
    assert dr.route_after_research({}, _cfg(facts_first_mode=False)) == "final_report_generation"


def test_route_after_extract_respects_facts_first(monkeypatch):
    monkeypatch.delenv("FACTS_FIRST_MODE", raising=False)
    assert dr.route_after_extract({}, _cfg(facts_first_mode=True)) == "assess_sufficiency"
    assert dr.route_after_extract({}, _cfg(facts_first_mode=False)) == "persist_research"


def test_default_config_is_report_mode():
    from open_deep_research.configuration import Configuration
    c = Configuration()
    assert c.facts_first_mode is False
    assert c.max_fact_rounds == 2


# -- target-property coverage + answer rendering (pure) ------------------------

def test_target_property_coverage():
    rows = [
        {"property_name": "foundational_id_scheme", "admission": "trusted", "in_conflict": False},
        {"property_name": "id_coverage_pct", "admission": "provisional", "in_conflict": False},
    ]
    present, trusted = dr._target_property_coverage(rows, ["foundational_id_scheme", "id_coverage_pct", "legal_basis"])
    assert present == {"foundational_id_scheme": True, "id_coverage_pct": True, "legal_basis": False}
    assert trusted["foundational_id_scheme"] is True and trusted["id_coverage_pct"] is False


def test_facts_answer_text_renders_present_and_missing():
    rows = [{"property_name": "foundational_id_scheme", "value": "aadhaar",
             "admission": "trusted", "in_conflict": False, "source_count": 15}]
    out = dr._facts_answer_text("India", rows, ["foundational_id_scheme", "id_coverage_pct"])
    assert "aadhaar (trusted, 15 sources)" in out
    assert "id_coverage_pct**: missing" in out


# -- resolve_target_properties (faked LLM) -------------------------------------

class _FakeChain:
    def __init__(self, result=None, raises=False):
        self._result, self._raises = result, raises

    def with_structured_output(self, *a, **k): return self
    def with_retry(self, *a, **k): return self
    def with_config(self, *a, **k): return self

    async def ainvoke(self, *a, **k):
        if self._raises:
            raise RuntimeError("model unavailable")
        return self._result


def test_resolve_target_properties_validates_and_drops_unknown(monkeypatch):
    monkeypatch.setattr(dr, "configurable_model",
                        _FakeChain(TargetProperties(property_names=["id_coverage_pct", "bogus_prop"])))
    out = asyncio.run(dr.resolve_target_properties("coverage?", DI, _CfgObj(), {}))
    assert out == ["id_coverage_pct"]  # bogus dropped


def test_resolve_target_properties_falls_back_to_all_on_failure(monkeypatch):
    monkeypatch.setattr(dr, "configurable_model", _FakeChain(raises=True))
    out = asyncio.run(dr.resolve_target_properties("anything", DI, _CfgObj(), {}))
    assert set(out) == {pd.name for pd in DI.properties}  # all properties


class _CfgObj:
    """Minimal stand-in for Configuration with the fields resolve_target_properties reads."""
    max_structured_output_retries = 1
    summarization_model = "gemini:flash"
    summarization_model_max_tokens = 1024

    def model_chain(self, role, step=None):
        # mirror Configuration.model_chain's single-model fallback for the test
        model = getattr(self, f"{role}_model", None)
        return [model] if model else []


# -- assess_sufficiency (DB-seeded, no LLM) ------------------------------------

async def _seed_fact(db, property_name, instance_key="IND"):
    async with aiosqlite.connect(db) as conn:
        await conn.executescript("CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)
        await conn.execute(
            "INSERT INTO fact (tuple_key, instance_key, property_name, value, canonical_value, "
            "admission, lifecycle) VALUES ('t',?,?,?,?, 'trusted','current')",
            (instance_key, property_name, "X", "x"))
        await conn.commit()


def test_assess_sufficiency_routes_to_answer_when_covered(tmp_path):
    db = str(tmp_path / "s.db")

    async def run():
        await _seed_fact(db, "foundational_id_scheme")
        state = {"subject": "India", "target_properties": ["foundational_id_scheme"], "fact_rounds_used": 0}
        cmd = await dr.assess_sufficiency(state, _cfg(database_path=db, max_fact_rounds=2))
        assert cmd.goto == "answer_from_facts"
    asyncio.run(run())


def test_assess_sufficiency_loops_back_on_gap_within_budget(tmp_path):
    db = str(tmp_path / "s2.db")

    async def run():
        await _seed_fact(db, "foundational_id_scheme")  # present
        state = {"subject": "India",
                 "target_properties": ["foundational_id_scheme", "id_coverage_pct"],  # coverage missing
                 "fact_rounds_used": 0}
        cmd = await dr.assess_sufficiency(state, _cfg(database_path=db, max_fact_rounds=2))
        assert cmd.goto == "write_research_brief"
        assert "id_coverage_pct" in cmd.update["missing_information"]
        assert cmd.update["fact_rounds_used"] == 1
    asyncio.run(run())


def test_assess_sufficiency_answers_when_budget_exhausted(tmp_path):
    db = str(tmp_path / "s3.db")

    async def run():
        await _seed_fact(db, "foundational_id_scheme")
        state = {"subject": "India",
                 "target_properties": ["foundational_id_scheme", "id_coverage_pct"],
                 "fact_rounds_used": 1}  # already used round 1; max=2 -> 1+1<2 false
        cmd = await dr.assess_sufficiency(state, _cfg(database_path=db, max_fact_rounds=2))
        assert cmd.goto == "answer_from_facts"
    asyncio.run(run())


def test_facts_answer_text_consolidates_singular_property_to_best_value():
    # A singular `name` property with several conflicting source-variants must render ONE
    # value (best-corroborated; ties broken toward non-conflict then longest), not a dump.
    rows = [
        {"property_name": "foundational_id_scheme", "value": "digital",
         "admission": "provisional", "in_conflict": False, "source_count": 1},
        {"property_name": "foundational_id_scheme", "value": "Estonia's e-ID (electronic identity)",
         "admission": "provisional", "in_conflict": False, "source_count": 3},
        {"property_name": "foundational_id_scheme", "value": "personal id code",
         "admission": "provisional", "in_conflict": True, "source_count": 1},
    ]
    out = dr._facts_answer_text("Estonia", rows, ["foundational_id_scheme"],
                                singular_props={"foundational_id_scheme"})
    assert "Estonia's e-ID (electronic identity)" in out          # best (3 sources) kept
    assert "personal id code" not in out and "**: digital" not in out  # others dropped
    assert out.count("**foundational_id_scheme**") == 1


def test_facts_answer_text_keeps_all_values_for_non_singular_property():
    rows = [
        {"property_name": "biometric_capture", "value": "fingerprint",
         "admission": "trusted", "in_conflict": False, "source_count": 2},
        {"property_name": "biometric_capture", "value": "iris",
         "admission": "trusted", "in_conflict": False, "source_count": 2},
    ]
    out = dr._facts_answer_text("India", rows, ["biometric_capture"], singular_props=set())
    assert "fingerprint" in out and "iris" in out
