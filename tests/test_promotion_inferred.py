from open_deep_research.factbase import model, promotion


def _fact(**kw):
    base = dict(fact_id=1, tuple_key="t", as_of=None, value="true", unit=None,
                source_meets_bar=True, has_unspecified_required=False)
    base.update(kw)
    return model.Fact(**base)


def test_inferred_required_qualifier_blocks_promotion():
    f = _fact(has_inferred_required=True)
    assert promotion.evaluate(f, [f], has_open_conflict=False) is None  # not promoted


def test_stated_required_qualifier_still_promotes():
    f = _fact(has_inferred_required=False)
    assert isinstance(promotion.evaluate(f, [f], has_open_conflict=False), model.Promote)
