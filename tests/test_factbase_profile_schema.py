import pytest

from open_deep_research.factbase.profile_schema import profile_from_dict

VALID = {
    "entity_type": "country",
    "version": "1",
    "properties": [
        {
            "name": "scheme_status",
            "kind": "enum",
            "identity_qualifiers": ["basis"],
            "required_qualifiers": ["basis"],
            "qualifier_enums": {"basis": ["de_jure", "de_facto"]},
            "value_enum": [
                {"value": "operational", "description": "issuing at scale"},
                "mandatory",
            ],
        },
        {"name": "scheme_name", "kind": "name", "value_aliases": {"aadhaar": ["uidai"]}},
    ],
}


def test_valid_profile_builds_dataclass_with_enum_values_flattened():
    prof = profile_from_dict(VALID)
    assert prof.entity_type == "country"
    status = prof.property("scheme_status")
    assert status.value_enum == ["operational", "mandatory"]  # {value,...} flattened
    assert status.validate("mandatory") is True
    assert prof.property("scheme_name").aliases_for("uidai") == "aadhaar"


def test_unknown_kind_rejected():
    bad = {"entity_type": "country", "properties": [{"name": "x", "kind": "wat"}]}
    with pytest.raises(ValueError, match="unknown kind"):
        profile_from_dict(bad)


def test_value_enum_on_non_enum_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "value_enum": ["a"]}]}
    with pytest.raises(ValueError, match="value_enum only allowed"):
        profile_from_dict(bad)


def test_required_qualifier_not_in_identity_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "required_qualifiers": ["basis"]}]}
    with pytest.raises(ValueError, match="required_qualifiers"):
        profile_from_dict(bad)


def test_qualifier_enums_key_not_declared_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "qualifier_enums": {"basis": ["a"]}}]}
    with pytest.raises(ValueError, match="qualifier_enums"):
        profile_from_dict(bad)


def test_duplicate_property_names_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name"}, {"name": "x", "kind": "name"}]}
    with pytest.raises(ValueError, match="duplicate property"):
        profile_from_dict(bad)


def test_empty_entity_type_rejected():
    bad = {"entity_type": "  ", "properties": [{"name": "x", "kind": "name"}]}
    with pytest.raises(ValueError, match="entity_type"):
        profile_from_dict(bad)


def test_overlapping_value_aliases_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "name", "value_aliases": {"a": ["dup"], "b": ["dup"]}}]}
    with pytest.raises(ValueError, match="multiple canonicals"):
        profile_from_dict(bad)
