import json
from open_deep_research.factbase.promotion import has_inferred_required_qualifier


def test_inferred_provenance_sets_flag():
    assert has_inferred_required_qualifier(json.dumps({"stage": "inferred"})) is True


def test_stated_or_empty_provenance_does_not():
    assert has_inferred_required_qualifier(json.dumps({"stage": "stated"})) is False
    assert has_inferred_required_qualifier("{}") is False
    assert has_inferred_required_qualifier(None) is False
