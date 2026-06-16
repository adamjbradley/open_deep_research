from open_deep_research.configuration import Configuration


def test_profile_name_defaults():
    c = Configuration()
    assert c.profile_name == "country_digital_identity"
    assert c.registry_name == "di_source_registry"


def test_profile_name_overridable_via_runnable_config():
    c = Configuration.from_runnable_config({"configurable": {"profile_name": "country_cbdc"}})
    assert c.profile_name == "country_cbdc"
