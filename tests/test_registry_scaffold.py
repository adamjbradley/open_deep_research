import asyncio

from open_deep_research.factbase.registry_scaffold import (
    RegistryProposal, induce_registry, render_registry_draft_yaml, render_registry_yaml)
from open_deep_research.factbase.registry_schema import registry_from_dict


def _proposal():
    return RegistryProposal(sources=[
        {"domain": "cbn.gov.ng", "tier": "authoritative", "flags": ["primary"],
         "rationale": "national central bank, primary issuer", "confidence": "high"},
        {"domain": "randomblog.example", "tier": "unvetted", "flags": [],
         "rationale": "unknown provenance", "confidence": "low"},
    ])


def test_induce_validates_against_registry_meta_schema():
    async def fake_call(prompt):
        return _proposal()
    out = asyncio.run(induce_registry("cbdc", "central bank digital currency", [], fake_call))
    assert any(s.domain == "cbn.gov.ng" for s in out.sources)


def test_render_yaml_is_loadable_registry():
    import yaml as _y
    yml = render_registry_yaml(_proposal())
    entries = registry_from_dict(_y.safe_load(yml))   # meta-schema gate
    assert entries["cbn.gov.ng"]["tier"] == "authoritative"


def test_draft_has_annotations():
    d = render_registry_draft_yaml(_proposal())
    assert "SCAFFOLD DRAFT" in d
    assert "cbn.gov.ng" in d and "confidence: high" in d
