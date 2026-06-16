from open_deep_research.factbase import profile
from open_deep_research.factbase.profile_schema import profile_from_dict


def test_description_populated_from_dict():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "p", "kind": "name", "description": "hello world"}],
    })
    assert prof.property("p").description == "hello world"


def test_description_defaults_empty_and_real_profile_has_descriptions():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "p", "kind": "name"}],
    })
    assert prof.property("p").description == ""
    real = profile.load("country_digital_identity")
    assert real.property("scheme_status").description  # the YAML sets one
