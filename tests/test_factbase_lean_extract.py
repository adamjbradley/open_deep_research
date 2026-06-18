from open_deep_research.factbase.lean_extract import LeanFact, slot_qualifiers
from open_deep_research.factbase.profile_schema import profile_from_dict

PROF = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "cov", "kind": "percentage",
     "identity_qualifiers": ["population_basis", "coverage_kind", "measured_modeled"],
     "qualifier_enums": {"population_basis": ["adults_15plus", "total_pop"],
                         "coverage_kind": ["enrolled", "issued"],
                         "measured_modeled": ["measured", "modeled"]}},
]})
PD = PROF.property("cov")

def test_slot_assigns_each_token_to_its_qualifier():
    out = slot_qualifiers(PD, ["total_pop", "issued", "measured"])
    assert out == {"population_basis": "total_pop", "coverage_kind": "issued",
                   "measured_modeled": "measured"}

def test_slot_drops_unknown_tokens_and_is_case_insensitive():
    assert slot_qualifiers(PD, ["TOTAL_POP", "nonsense"]) == {"population_basis": "total_pop"}

def test_slot_unset_qualifier_when_no_token():
    assert slot_qualifiers(PD, ["issued"]) == {"coverage_kind": "issued"}

def test_leanfact_qualifiers_is_a_flat_list():
    f = LeanFact(property="cov", instance_name="Estonia", value="99",
                 evidence_span="99% hold", qualifiers=["total_pop"])
    assert f.qualifiers == ["total_pop"]
