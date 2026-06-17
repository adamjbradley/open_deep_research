from open_deep_research.factbase import profile
from open_deep_research.factbase.profile_schema import profile_from_dict


def _prop(**kw):
    base = {"name": "p", "kind": "enum", "value_enum": ["photo", "fingerprint", "iris"]}
    base.update(kw)
    return profile_from_dict({"entity_type": "c", "properties": [base]}).property("p")


def test_multi_closed_accepts_valid_subset_rejects_junk():
    p = _prop(multi=True)
    assert p.validate("fingerprint, iris") is True
    assert p.validate("iris,photo") is True
    assert p.validate("fingerprint, asdf") is False     # one bad member rejects whole fact
    assert p.validate("") is True                        # empty set == none captured


def test_multi_open_keeps_unknown_member():
    p = _prop(multi=True, open=True)
    assert p.validate("fingerprint, palmprint") is True  # palmprint not in enum, allowed


def test_single_open_accepts_literal_outside_enum():
    p = _prop(open=True)
    assert p.validate("hub") is True                     # not in enum, allowed by open
    assert p.validate("photo") is True


def test_single_closed_unchanged():
    p = _prop()
    assert p.validate("photo") is True
    assert p.validate("hub") is False


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
