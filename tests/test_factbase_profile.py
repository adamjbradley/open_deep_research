from open_deep_research.factbase import profile


def test_di_profile_has_expected_properties_with_qualifiers():
    p = profile.load("country_digital_identity")
    names = {pd.name for pd in p.properties}
    assert {"foundational_id_scheme", "scheme_status", "id_coverage_pct", "biometric_capture", "data_protection_law", "legal_basis"} <= names
    cov = p.property("id_coverage_pct")
    assert set(cov.identity_qualifiers) == {"population_basis", "coverage_kind", "measured_modeled"}
    assert "registered_holders" in cov.qualifier_enums["population_basis"]


def test_validation_rejects_out_of_range_percentage():
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    assert cov.validate("412") is False
    assert cov.validate("87") is True


def test_coverage_required_qualifiers_is_population_basis_only():
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    assert cov.required_qualifiers == ["population_basis"]
