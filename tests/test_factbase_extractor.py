import asyncio

from open_deep_research.factbase import extractor, profile
from open_deep_research.factbase.profile_schema import profile_from_dict

DI = profile.load("country_digital_identity")


def _raw(records):
    async def _call(source_text, prof):
        return records
    return _call


def test_keeps_valid_span_verified_record():
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%", "as_of": "2024",
           "qualifiers": {"population_basis": "adults_15plus"}, "evidence_span": "coverage reached 99%"}
    out = asyncio.run(extractor.extract("India coverage reached 99% in 2024", DI, _raw([rec])))
    assert len(out) == 1 and out[0]["value"] == "99"
    assert isinstance(out[0]["qualifiers"], dict)
    assert out[0]["qualifiers"] == {"population_basis": "adults_15plus"}


def test_drops_unverifiable_span():
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "42", "unit": "%", "as_of": "2024",
           "qualifiers": {"population_basis": "adults_15plus"}, "evidence_span": "coverage was 42%"}
    out = asyncio.run(extractor.extract("India coverage reached 99% in 2024", DI, _raw([rec])))
    assert out == []


def test_drops_value_failing_validation():
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "412", "unit": "%", "as_of": "2024",
           "qualifiers": {}, "evidence_span": "412"}
    out = asyncio.run(extractor.extract("nonsense 412", DI, _raw([rec])))
    assert out == []


# ---------------------------------------------------------------------------
# New tests: lean extraction (qualifiers as list[str] -> slotted dict)
# ---------------------------------------------------------------------------

PROF_LEAN = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "cov", "kind": "percentage", "identity_qualifiers": ["population_basis"],
     "qualifier_enums": {"population_basis": ["total_pop"]}},
]})
SRC_LEAN = "Estonia: 99% of the total population hold the ID."


def test_extract_reconstructs_factrecord_with_slotted_qualifiers():
    async def model_call(source_text, prof):   # returns LEAN dicts (qualifiers as a list)
        return [{"property": "cov", "instance_name": "Estonia", "value": "99",
                 "evidence_span": "99% of the total population hold the ID",
                 "qualifiers": ["total_pop"]}]
    recs = asyncio.run(extractor.extract(SRC_LEAN, PROF_LEAN, model_call))
    assert len(recs) == 1
    assert recs[0]["qualifiers"] == {"population_basis": "total_pop"}   # list -> dict (back-compat shape)
    assert recs[0]["value"] == "99"


def test_extract_drops_ungrounded_evidence_span():
    async def model_call(s, p):
        return [{"property": "cov", "instance_name": "Estonia", "value": "50",
                 "evidence_span": "this text is NOT in the source", "qualifiers": []}]
    assert asyncio.run(extractor.extract(SRC_LEAN, PROF_LEAN, model_call)) == []


# ---------------------------------------------------------------------------
# C1: Unicode normalization (_norm handles NBSP + curly quotes)
# ---------------------------------------------------------------------------

from open_deep_research.factbase.extractor import _norm


def test_norm_unicode_nbsp_and_quotes():
    src = "Coverage is “99%” as of 2023"   # NBSP + curly quotes
    span = 'Coverage is "99%" as of 2023'                 # plain space + straight quotes
    assert _norm(span) in _norm(src)


# ---------------------------------------------------------------------------
# C2: Fuzzy span fallback (_span_present for near-miss quotes)
# ---------------------------------------------------------------------------

from open_deep_research.factbase.extractor import _span_present


def test_span_present_accepts_near_paraphrase():
    src = _norm("aadhaar is brazil's foundational identity scheme operated by uidai")
    near = _norm("aadhaar is brazil's foundational identity scheme operated by the uidai")  # tiny diff
    far = _norm("the moon is made of cheese and has no relation to identity systems at all")
    assert _span_present(near, src) is True
    assert _span_present(far, src) is False
