from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.completeness import assess_property_status, is_complete

PROF = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "scheme", "kind": "name",
     "narrative": {"required": True, "guidance": "g"}, "absence_allowed": False},
    {"name": "cov", "kind": "percentage",
     "identity_qualifiers": ["population_basis"], "required_qualifiers": ["population_basis"]},
]})

def _row(p, value="x", quals=None, narrative="", source_count=2, admission="trusted"):
    return {"property_name": p, "value": value, "qualifiers": quals or {},
            "narrative": narrative, "source_count": source_count, "admission": admission}

def test_resolved_when_value_qualifiers_and_required_narrative_present():
    rows = [_row("scheme", narrative="how it works")]
    st = assess_property_status(rows, set(), PROF)
    assert st["scheme"] == "resolved"

def test_missing_value():
    assert assess_property_status([], set(), PROF)["scheme"] == "missing_value"

def test_missing_required_narrative():
    rows = [_row("scheme", narrative="")]
    assert assess_property_status(rows, set(), PROF)["scheme"] == "missing_narrative"

def test_missing_required_qualifier():
    rows = [_row("cov", quals={})]      # population_basis required, absent
    assert assess_property_status(rows, set(), PROF)["cov"] == "missing_qualifier"

def test_confirmed_absent_from_absent_set():
    st = assess_property_status([], {"cov"}, PROF)
    assert st["cov"] == "confirmed_absent"

def test_is_complete_honours_absence_allowed():
    pd_scheme = PROF.property("scheme")     # absence_allowed False
    pd_cov = PROF.property("cov")           # absence_allowed True (default)
    assert is_complete("resolved", pd_scheme) is True
    assert is_complete("confirmed_absent", pd_scheme) is False   # absence forbidden
    assert is_complete("confirmed_absent", pd_cov) is True
    assert is_complete("missing_value", pd_cov) is False
