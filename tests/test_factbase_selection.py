from open_deep_research.configuration import Configuration


def test_profile_name_defaults():
    c = Configuration()
    assert c.profile_name == "country_digital_identity"
    assert c.registry_name == "di_source_registry"


def test_profile_name_overridable_via_runnable_config():
    c = Configuration.from_runnable_config({"configurable": {"profile_name": "country_cbdc"}})
    assert c.profile_name == "country_cbdc"


def test_a_second_profile_drives_resolution(monkeypatch):
    import open_deep_research.factbase.profile as profile_mod

    real_load = profile_mod.load

    def fake_load(name):
        if name == "country_cbdc":
            from open_deep_research.factbase.profile_schema import profile_from_dict
            return profile_from_dict({
                "entity_type": "country", "version": "1",
                "properties": [{"name": "cbdc_status", "kind": "enum",
                                "value_enum": ["research", "pilot", "launched"]}],
            })
        return real_load(name)

    monkeypatch.setattr(profile_mod, "load", fake_load)
    p = profile_mod.load("country_cbdc")
    assert p.property("cbdc_status").value_enum == ["research", "pilot", "launched"]
