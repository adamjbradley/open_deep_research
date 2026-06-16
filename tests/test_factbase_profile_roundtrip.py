from open_deep_research.factbase import profile
from open_deep_research.factbase.profiles import country_digital_identity as py_mod


def _as_tuple(pd):
    return (
        pd.name, pd.value_kind,
        tuple(sorted(pd.identity_qualifiers)),
        tuple(sorted(pd.required_qualifiers)),
        tuple(sorted((k, tuple(sorted(v))) for k, v in pd.qualifier_enums.items())),
        None if pd.value_enum is None else tuple(sorted(pd.value_enum)),
        pd.trust_threshold,
        tuple(sorted((k, tuple(sorted(v))) for k, v in pd.value_aliases.items())),
    )


def test_yaml_profile_matches_python_profile():
    py = py_mod.PROFILE
    yaml_prof = profile.load("country_digital_identity")
    assert yaml_prof.entity_type == py.entity_type
    assert {_as_tuple(p) for p in yaml_prof.properties} == {_as_tuple(p) for p in py.properties}


def test_yaml_profile_preserves_aadhaar_aliases():
    cov = profile.load("country_digital_identity").property("foundational_id_scheme")
    assert cov.aliases_for("uidai") == "aadhaar"
    assert cov.aliases_for("aadhaar uid") == "aadhaar"
