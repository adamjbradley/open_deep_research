import pytest

from open_deep_research.factbase.registry import SourceRegistry
from open_deep_research.factbase.registry_schema import registry_from_dict


def test_yaml_registry_values():
    reg = SourceRegistry.load("di_source_registry")
    assert reg.tier("https://uidai.gov.in/x") == "reputable"
    assert reg.flags("https://id4d.worldbank.org/y") == ["modeled"]
    assert reg.meets_bar("https://gsma.com/z", "reputable") is True
    assert reg.tier("https://unknown.example/q") == "unvetted"


def test_invalid_tier_rejected():
    with pytest.raises(ValueError, match="tier"):
        registry_from_dict({"version": "1", "sources": [{"domain": "x.com", "tier": "gold"}]})


def test_registry_carries_version_and_semantic_hash():
    reg = SourceRegistry.load("di_source_registry")
    assert reg.registry_version == "1"
    assert isinstance(reg.registry_hash, str) and len(reg.registry_hash) == 64


def test_load_registry_hash_is_stable_and_content_sensitive():
    from open_deep_research.factbase.registry_schema import load_registry
    base = {"version": "1", "sources": [{"domain": "a.com", "tier": "reputable", "flags": ["x"]}]}
    _, v1, h1 = load_registry(base)
    _, v2, h2 = load_registry({"version": "1", "sources": [{"domain": "a.com", "tier": "reputable", "flags": ["x"]}]})
    assert (v1, h1) == (v2, h2)  # stable
    _, _, h3 = load_registry({"version": "1", "sources": [{"domain": "a.com", "tier": "authoritative"}]})
    assert h3 != h1  # tier change -> different hash
