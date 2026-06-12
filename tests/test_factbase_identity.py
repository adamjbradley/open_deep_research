from open_deep_research.factbase import identity


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
