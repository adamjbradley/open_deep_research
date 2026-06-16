import pytest

from open_deep_research.factbase.registry import SourceRegistry
from open_deep_research.factbase.registry_schema import registry_from_dict


def test_yaml_registry_matches_python_registry():
    from open_deep_research.factbase.profiles import di_source_registry as py_mod
    reg = SourceRegistry.load("di_source_registry")
    assert reg.tier("https://uidai.gov.in/x") == py_mod.ENTRIES["uidai.gov.in"]["tier"]
    assert reg.flags("https://id4d.worldbank.org/y") == ["modeled"]
    assert reg.meets_bar("https://gsma.com/z", "reputable") is True
    assert reg.tier("https://unknown.example/q") == "unvetted"


def test_invalid_tier_rejected():
    with pytest.raises(ValueError, match="tier"):
        registry_from_dict({"version": "1", "sources": [{"domain": "x.com", "tier": "gold"}]})
