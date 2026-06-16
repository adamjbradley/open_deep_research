from open_deep_research.factbase.identity import canonical_value
from open_deep_research.factbase.profile import PropertyDef
from open_deep_research.factbase.profile_schema import profile_from_dict


def test_meta_schema_accepts_number_kind():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "population", "kind": "number", "description": "total"}],
    })
    assert prof.property("population").value_kind == "number"


def test_validate_number_accepts_separators_rejects_text():
    pd = PropertyDef(name="population", value_kind="number")
    assert pd.validate("1402000000") is True
    assert pd.validate("1,402,000,000") is True
    assert pd.validate("  1_402_000_000 ") is True
    assert pd.validate("12.5") is True
    assert pd.validate("abc") is False
    assert pd.validate("") is False


def test_canonical_number_collapses_separators_and_integral():
    pd = PropertyDef(name="population", value_kind="number")
    a, _ = canonical_value(pd, "1,402,000,000", None)
    b, _ = canonical_value(pd, "1402000000", None)
    assert a == b == "1402000000"          # separators stripped, integral form
    c, _ = canonical_value(pd, "12.50", None)
    assert c == "12.5"                       # non-integral normalized
    d, _ = canonical_value(pd, "not a number", None)
    assert d == "not a number"              # non-numeric falls back to text norm
