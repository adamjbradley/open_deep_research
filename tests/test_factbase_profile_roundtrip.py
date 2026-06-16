from open_deep_research.factbase import profile

# Frozen expectation captured from the original Python profile at migration time.
EXPECTED = {
    "foundational_id_scheme": ("name", [], [], {}, None),
    "scheme_status": ("enum", ["basis"], ["basis"], {"basis": ["de_facto", "de_jure"]},
                      ["announced", "mandatory", "operational", "piloting"]),
    "id_coverage_pct": ("percentage",
                        ["coverage_kind", "measured_modeled", "population_basis"],
                        ["population_basis"],
                        {"coverage_kind": ["active", "enrolled", "issued"],
                         "measured_modeled": ["measured", "modeled"],
                         "population_basis": ["adults_15plus", "births", "registered_holders", "total_pop"]},
                        None),
    "biometric_capture": ("enum", [], [], {},
                          ["fingerprint", "iris", "multi", "none", "photo"]),
    "data_protection_law": ("boolean", ["jurisdiction", "scope", "stage"], ["stage"],
                            {"scope": ["comprehensive", "sectoral"], "stage": ["enacted", "in_force"]},
                            None),
    "legal_basis": ("name_year", ["jurisdiction"], [], {}, None),
}


def test_yaml_profile_matches_frozen_snapshot():
    prof = profile.load("country_digital_identity")
    assert prof.entity_type == "country"
    got = {}
    for pd in prof.properties:
        got[pd.name] = (
            pd.value_kind, sorted(pd.identity_qualifiers), sorted(pd.required_qualifiers),
            {k: sorted(v) for k, v in pd.qualifier_enums.items()},
            None if pd.value_enum is None else sorted(pd.value_enum),
        )
    assert got == EXPECTED


def test_yaml_profile_preserves_aadhaar_aliases():
    scheme = profile.load("country_digital_identity").property("foundational_id_scheme")
    assert scheme.aliases_for("uidai") == "aadhaar"
    assert scheme.aliases_for("aadhaar uid") == "aadhaar"
