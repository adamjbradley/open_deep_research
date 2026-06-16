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
