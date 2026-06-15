import asyncio

from open_deep_research.factbase import extractor, profile

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
