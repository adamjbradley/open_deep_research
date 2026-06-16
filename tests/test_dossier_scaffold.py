import asyncio

import yaml

from open_deep_research.factbase import dossier, scaffold
from open_deep_research.factbase.profile_schema import profile_from_dict


def _stub_model_call():
    async def call(prompt):
        return scaffold.ScaffoldProposal(entity_type="country", properties=[
            {"name": "cbdc_status", "kind": "enum", "description": "maturity",
             "identity_qualifiers": ["basis"], "required_qualifiers": ["basis"],
             "qualifier_enums": {"basis": ["de_jure", "de_facto"]},
             "value_enum": ["research", "pilot", "launched"],
             "identity_rationale": "legal vs practical", "confidence": "medium"}])
    return call


def test_scaffold_writes_usable_profile_and_comparison_draft(tmp_path, monkeypatch):
    out_yaml = tmp_path / "country_cbdc.yaml"
    out_draft = tmp_path / "country_cbdc.draft.yaml"
    monkeypatch.setattr(dossier, "_scaffold_model_call", _stub_model_call)

    msg = asyncio.run(dossier.run(
        ["scaffold", "country", "CBDC programs", "--out", str(out_yaml)]))

    # Usable profile: clean (no review comments), loadable, re-validates.
    assert out_yaml.exists()
    ytext = out_yaml.read_text()
    assert "SCAFFOLD DRAFT" not in ytext
    prof = profile_from_dict(yaml.safe_load(ytext))
    assert prof.property("cbdc_status").value_enum == ["research", "pilot", "launched"]

    # Comparison draft: annotated, present, NOT the usable file (validate skips *.draft.yaml).
    assert out_draft.exists()
    assert "SCAFFOLD DRAFT" in out_draft.read_text()
    assert "live now" in msg
    # The draft is NOT the loadable profile (annotated; *.draft.yaml is skipped by validate/load).
    assert out_draft.read_text() != ytext


def test_scaffold_seed_fetches_and_grounds_prompt(tmp_path, monkeypatch):
    out_yaml = tmp_path / "country_x.yaml"
    captured = {}

    async def capturing_model_call(prompt):
        captured["prompt"] = prompt
        return scaffold.ScaffoldProposal(
            entity_type="country", properties=[{"name": "x", "kind": "name"}])

    async def fake_fetch_text(url, **kw):
        return "SEEDED DOMAIN VOCABULARY about widgets and gizmos"

    monkeypatch.setattr(dossier, "_scaffold_model_call", lambda: capturing_model_call)
    from open_deep_research.factbase import fetch as _fetch
    monkeypatch.setattr(_fetch, "fetch_text", fake_fetch_text)

    asyncio.run(dossier.run(
        ["scaffold", "country", "widget domain", "--seed", "https://x.example/a",
         "--seed", "https://x.example/b", "--out", str(out_yaml)]))

    # The fetched seed text must have grounded the generation prompt (as data).
    assert "SEEDED DOMAIN VOCABULARY about widgets and gizmos" in captured["prompt"]
    assert "treat as DATA" in captured["prompt"] or "never as instructions" in captured["prompt"]
    assert out_yaml.exists()


def test_scaffold_skips_unreachable_seeds(tmp_path, monkeypatch):
    out_yaml = tmp_path / "country_y.yaml"
    captured = {}

    async def capturing_model_call(prompt):
        captured["prompt"] = prompt
        return scaffold.ScaffoldProposal(
            entity_type="country", properties=[{"name": "x", "kind": "name"}])

    async def fake_fetch_text(url, **kw):
        return None  # fetch_text returns None on any failure (SSRF/timeout/non-HTML)

    monkeypatch.setattr(dossier, "_scaffold_model_call", lambda: capturing_model_call)
    from open_deep_research.factbase import fetch as _fetch
    monkeypatch.setattr(_fetch, "fetch_text", fake_fetch_text)

    # A failed fetch is dropped; scaffolding still succeeds (description-only).
    asyncio.run(dossier.run(
        ["scaffold", "country", "domain", "--seed", "https://bad.example", "--out", str(out_yaml)]))
    assert out_yaml.exists()
    assert "SEED SOURCE TEXT" not in captured["prompt"]  # no seed block when all fetches failed


def test_scaffold_reuses_existing_entity_properties_in_prompt(tmp_path, monkeypatch):
    out_yaml = tmp_path / "country_more.yaml"
    captured = {}

    async def capturing(prompt):
        captured["prompt"] = prompt
        return scaffold.ScaffoldProposal(entity_type="country", properties=[{"name": "z", "kind": "name"}])

    monkeypatch.setattr(dossier, "_scaffold_model_call", lambda: capturing)
    asyncio.run(dossier.run(["scaffold", "country", "more country facts", "--out", str(out_yaml)]))
    # The country entity already has DI properties -> the prompt tells the model not to re-propose them.
    assert "already exists" in captured["prompt"]
    assert "scheme_status" in captured["prompt"]
