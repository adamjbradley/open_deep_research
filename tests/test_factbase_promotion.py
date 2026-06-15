from open_deep_research.factbase import promotion, model


def _f(fid, value, bar=True, unspec=False):
    return model.Fact(fid, "t", 2024, value, "%", bar, unspec)


def test_promote_when_bar_met_no_conflict_no_unspecified():
    f = _f(1, "99")
    assert promotion.evaluate(f, [f], has_open_conflict=False) == model.Promote(1)


def test_no_promote_when_below_bar():
    f = _f(1, "99", bar=False)
    assert promotion.evaluate(f, [f], has_open_conflict=False) is None


def test_no_promote_when_unspecified_qualifier():
    f = _f(1, "99", unspec=True)
    assert promotion.evaluate(f, [f], has_open_conflict=False) is None


def test_no_promote_when_open_conflict_in_bucket():
    f = _f(1, "99")
    assert promotion.evaluate(f, [f, _f(2, "87")], has_open_conflict=True) is None


def test_trusted_fact_demoted_when_conflict_opens():
    f = _f(1, "99")
    f.admission = "trusted"
    assert promotion.evaluate(f, [f, _f(2, "87")], has_open_conflict=True) == model.Demote(1)
