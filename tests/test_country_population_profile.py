from open_deep_research.factbase.profile import load as load_profile
from open_deep_research.factbase.registry import SourceRegistry


def test_population_profile_loads():
    prof = load_profile("country_population")
    assert prof.entity_type == "country"
    assert prof.property("population").value_kind == "number"


def test_population_registry_tiers_world_bank_authoritative():
    reg = SourceRegistry.load("country_population_source_registry")
    assert reg.tier("https://data.worldbank.org/indicator/SP.POP.TOTL") == "authoritative"
    assert reg.meets_bar("https://data.worldbank.org/x", "reputable") is True
