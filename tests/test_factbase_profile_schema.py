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


def test_multi_and_open_flags_build_and_surface():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [
            {"name": "biometric_capture", "kind": "enum", "multi": True,
             "value_enum": ["photo", "fingerprint", "iris", "face"]},
            {"name": "role", "kind": "enum", "open": True,
             "value_enum": ["sender", "receiver"]},
        ],
    })
    bio = prof.property("biometric_capture")
    assert bio.multi is True and bio.open_world is False
    role = prof.property("role")
    assert role.open_world is True and role.multi is False


def test_multi_on_non_enum_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "name", "multi": True}]}
    with pytest.raises(ValueError, match="multi.*only allowed for kind 'enum'"):
        profile_from_dict(bad)


def test_open_without_value_enum_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "enum", "open": True}]}
    with pytest.raises(ValueError, match="open.*requires value_enum"):
        profile_from_dict(bad)


def test_toggling_multi_changes_semantic_hash():
    base = {"entity_type": "country", "properties": [
        {"name": "b", "kind": "enum", "value_enum": ["photo", "iris"]}]}
    multi = {"entity_type": "country", "properties": [
        {"name": "b", "kind": "enum", "multi": True, "value_enum": ["photo", "iris"]}]}
    assert profile_from_dict(base).profile_hash != profile_from_dict(multi).profile_hash


def test_open_on_non_enum_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "name", "open": True}]}
    with pytest.raises(ValueError, match="open.*only allowed for kind 'enum'"):
        profile_from_dict(bad)


def test_toggling_open_changes_semantic_hash():
    base = {"entity_type": "country", "properties": [
        {"name": "r", "kind": "enum", "value_enum": ["sender", "receiver"]}]}
    open_ = {"entity_type": "country", "properties": [
        {"name": "r", "kind": "enum", "open": True, "value_enum": ["sender", "receiver"]}]}
    assert profile_from_dict(base).profile_hash != profile_from_dict(open_).profile_hash


def test_profile_parses_narrative_and_completeness_fields():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "narrative": {"overview_sections": ["How it works", "Coverage gaps"]},
        "properties": [
            {"name": "scheme", "kind": "name",
             "narrative": {"required": True, "guidance": "Explain enrolment + caveats."},
             "completeness": "required", "absence_allowed": False},
            {"name": "bio", "kind": "enum", "value_enum": ["photo"], "multi": True},
        ],
    })
    p = prof.property("scheme")
    assert p.narrative_required is True
    assert "enrolment" in p.narrative_guidance
    assert p.completeness == "required" and p.absence_allowed is False
    assert prof.overview_sections == ["How it works", "Coverage gaps"]
    # defaults when omitted:
    b = prof.property("bio")
    assert b.narrative_required is False and b.completeness == "required" and b.absence_allowed is True


def test_back_compat_profile_without_new_fields():
    prof = profile_from_dict({"entity_type": "country", "version": "1",
                              "properties": [{"name": "x", "kind": "name"}]})
    assert prof.overview_sections == []
    assert prof.property("x").narrative_required is False
