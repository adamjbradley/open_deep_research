from open_deep_research.factbase import entities, identity, profile, conflict, promotion, model


def test_two_sources_same_qualifiers_conflict_blocks_promotion():
    r = entities.CountryResolver()
    iid = r.resolve("India")
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    quals = {"population_basis": "adults_15plus", "coverage_kind": "enrolled", "measured_modeled": "measured"}
    tk = identity.tuple_key(hash(iid) & 0xffff, cov.name, quals)
    assert cov.validate("99") and cov.validate("87")
    facts = [
        model.Fact(1, tk, 2024, "99", "%", source_meets_bar=True, has_unspecified_required=False),
        model.Fact(2, tk, 2024, "87", "%", source_meets_bar=True, has_unspecified_required=False),
    ]
    conflicts = conflict.detect(facts)
    assert any(isinstance(i, model.OpenConflict) for i in conflicts)
    assert promotion.evaluate(facts[0], facts, has_open_conflict=True) is None


def test_different_denominator_is_not_a_conflict():
    cov = "id_coverage_pct"
    tk_a = identity.tuple_key(1, cov, {"population_basis": "adults_15plus"})
    tk_b = identity.tuple_key(1, cov, {"population_basis": "registered_holders"})
    a = model.Fact(1, tk_a, 2024, "99", "%", True, False)
    b = model.Fact(2, tk_b, 2024, "60", "%", True, False)
    assert conflict.detect([a]) == [] and conflict.detect([b]) == []
    assert tk_a != tk_b
