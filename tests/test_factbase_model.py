from open_deep_research.factbase import model


def test_fact_defaults_provisional_current():
    f = model.Fact(fact_id=1, tuple_key="t", as_of=2024, value="99", unit="%", source_meets_bar=True, has_unspecified_required=False)
    assert f.admission == "provisional" and f.lifecycle == "current"


def test_intents_carry_their_payload():
    assert model.Promote(5).fact_id == 5
    assert model.OpenConflict("t", 2024, [1, 2]).fact_ids == [1, 2]
