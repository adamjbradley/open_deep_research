from open_deep_research.factbase import identity, profile

_DI = profile.load("country_digital_identity")
_SCHEME = _DI.property("foundational_id_scheme")
_PCT = _DI.property("id_coverage_pct")
_LAW = _DI.property("legal_basis")
_STATUS = _DI.property("scheme_status")


def _cv(pd, value, unit=None):
    return identity.canonical_value(pd, value, unit)


def test_canonical_value_percentage_collapses_variants():
    assert _cv(_PCT, "~99")[0] == "99"
    assert _cv(_PCT, "99%")[0] == "99"
    assert _cv(_PCT, "99 percent")[0] == "99"
    assert _cv(_PCT, "about 99")[0] == "99"
    assert _cv(_PCT, "~99") == _cv(_PCT, "99%") == ("99", "%")


def test_canonical_value_percentage_out_of_range_falls_back_no_raise():
    # 412 is not a valid percent -> text fallback (stays distinct, never raises).
    assert _cv(_PCT, "412%")[0] != "412"
    assert _cv(_PCT, "n/a")  # no exception


def test_canonical_value_name_collapses_scheme_variants():
    base = _cv(_SCHEME, "Aadhaar")[0]
    assert base == "aadhaar"
    assert _cv(_SCHEME, "Aadhaar Card")[0] == base          # noise-word strip
    assert _cv(_SCHEME, "Unique Identity (UID) scheme or Aadhaar")[0] == base  # alias
    assert _cv(_SCHEME, "UIDAI")[0] == base                 # alias


def test_canonical_value_name_year_collapses_act_variants():
    base = _cv(_LAW, "Aadhaar Act")[0]
    assert _cv(_LAW, "Aadhaar Act, 2016")[0] == base
    assert _cv(_LAW, "Aadhaar (Targeted Delivery of Financial and other Subsidies, "
                     "Benefits and Services) Act, 2016")[0] == base


def test_canonical_value_does_not_overmerge():
    # Different acts stay distinct; "statutory authority" is NOT merged into the Act.
    assert _cv(_LAW, "Aadhaar Act")[0] != _cv(_LAW, "IT Act")[0]
    assert _cv(_LAW, "Statutory authority")[0] != _cv(_LAW, "Aadhaar Act")[0]
    # An out-of-enum value does not silently collapse into an enum member.
    assert _cv(_STATUS, "operational")[0] == "operational"
    assert _cv(_STATUS, "fully rolled out")[0] != "operational"


def test_canonicalize_normalizes_whitespace_and_case_within_same_unit():
    assert identity.canonicalize("  99 ", "%") == identity.canonicalize("99", "%")
    assert identity.canonicalize("Aadhaar", None) == identity.canonicalize("aadhaar", None)


def test_values_equal_true_for_same_normalized_value_and_unit():
    assert identity.values_equal("99", "%", "99", "%") is True


def test_values_equal_false_for_different_value():
    assert identity.values_equal("99", "%", "87", "%") is False


def test_values_equal_false_for_different_unit_no_normalization_in_v1():
    assert identity.values_equal("5", "mg/L", "5", "umol/L") is False


def test_tuple_key_excludes_as_of_and_orders_qualifiers():
    k1 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus", "coverage_kind": "enrolled"})
    k2 = identity.tuple_key(7, "id_coverage_pct", {"coverage_kind": "enrolled", "population_basis": "adults_15plus"})
    assert k1 == k2


def test_tuple_key_differs_by_qualifier_value():
    k1 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus"})
    k2 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "registered_holders"})
    assert k1 != k2


def test_tuple_key_unspecified_qualifier_is_its_own_tuple():
    specified = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus"})
    unspec = identity.tuple_key(7, "id_coverage_pct", {"population_basis": None})
    assert specified != unspec
