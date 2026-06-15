from open_deep_research.factbase import conflict, model


def _f(fid, value, as_of=2024, bar=True, tk="t"):
    return model.Fact(fid, tk, as_of, value, "%", bar, False)


def test_two_trust_bar_values_same_bucket_open_conflict():
    intents = conflict.detect([_f(1, "99"), _f(2, "87")])
    opens = [i for i in intents if isinstance(i, model.OpenConflict)]
    assert len(opens) == 1 and sorted(opens[0].fact_ids) == [1, 2]


def test_same_value_no_conflict():
    assert conflict.detect([_f(1, "99"), _f(2, "99")]) == []


def test_different_as_of_is_not_a_conflict():
    assert conflict.detect([_f(1, "99", as_of=2023), _f(2, "87", as_of=2024)]) == []


def test_lower_tier_disagreement_does_not_open_conflict():
    assert conflict.detect([_f(1, "99", bar=True), _f(2, "87", bar=False)]) == []


def test_collapse_to_one_value_auto_closes():
    intents = conflict.detect([_f(1, "99"), _f(2, "99")], had_open_conflict=True)
    assert any(isinstance(i, model.AutoClose) for i in intents)
