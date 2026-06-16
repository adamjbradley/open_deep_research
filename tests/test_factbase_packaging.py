from importlib.resources import files


def test_profile_yaml_is_packaged():
    pkg = files("open_deep_research.factbase.profiles")
    assert pkg.joinpath("country_digital_identity.yaml").is_file()
    assert pkg.joinpath("di_source_registry.yaml").is_file()


def test_load_works_via_resources():
    from open_deep_research.factbase import profile
    from open_deep_research.factbase.registry import SourceRegistry
    assert profile.load("country_digital_identity").entity_type == "country"
    assert SourceRegistry.load("di_source_registry").tier("https://gsma.com") == "authoritative"
