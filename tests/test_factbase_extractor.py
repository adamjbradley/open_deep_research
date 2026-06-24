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


# ---------------------------------------------------------------------------
# Follow-up 3: condensed long-quote span match (statute / legal sources)
#
# A model reading dense legal prose emits an evidence_span that lightly condenses a
# long passage -- dropping mid-quote clauses/markers (e.g. "(1)", "of the European
# Parliament and of the Council"). That span is a near-subsequence of a source region
# LONGER than any equal-length window, so the char-window fuzzy misses it even though
# it is genuinely grounded. It must be accepted, while fabricated/scattered spans
# (which are NOT in-order subsequences of the source) stay rejected.
# ---------------------------------------------------------------------------

_STATUTE = _norm(
    "§ 1. Scope of application of Act. (1) This Act lays down the conditions and procedure "
    "for the protection of natural persons upon the processing of personal data, and the procedure "
    "for the exercise of state supervision upon the processing of personal data, to the extent that "
    "the processing of personal data is governed by Regulation (EU) 2016/679 of the European "
    "Parliament and of the Council (General Data Protection Regulation)."
)


def test_span_present_accepts_condensed_long_statute_quote():
    # model dropped the "(1)" marker and the "of the European Parliament and of the Council" clause
    condensed = _norm(
        "This Act lays down the conditions and procedure for the protection of natural persons "
        "upon the processing of personal data, to the extent that the processing of personal data "
        "is governed by Regulation (EU) 2016/679 (General Data Protection Regulation)."
    )
    assert condensed not in _STATUTE  # not an exact substring -> exercises the new fallback
    assert _span_present(condensed, _STATUTE) is True


def test_span_present_rejects_hallucinated_legal_span():
    halluc = _norm(
        "The Act mandates biometric fingerprint collection for all national identity cards and "
        "requires annual security audits by the Data Protection Inspectorate."
    )
    assert _span_present(halluc, _STATUTE) is False


def test_span_present_rejects_scattered_token_subsequence():
    # every token appears somewhere in the source, but not as a contiguous in-order quote
    scattered = _norm(
        "application data Parliament identity trust Regulation supervision Council database protection"
    )
    assert _span_present(scattered, _STATUTE) is False


def test_extract_keeps_data_protection_law_from_condensed_statute_quote():
    # Regression for Follow-up 3: dense statute prose + a lightly-condensed evidence span
    # previously landed 0 data_protection_law facts because span verification dropped them.
    statute = (
        "§ 2. The processing of personal data in the national identity documents database and in "
        "connection with electronic identification and trust services is subject to this Act and to "
        "the said Regulation (EU) 2016/679 (General Data Protection Regulation)."
    )
    rec = {
        "property": "data_protection_law", "instance_name": "Estonia", "value": "true",
        "evidence_span": (
            "the processing of personal data in the national identity documents database in "
            "connection with electronic identification is subject to this Act and to the said Regulation"
        ),
        "qualifiers": ["in_force"],
    }
    out = asyncio.run(extractor.extract(statute, DI, _raw([rec])))
    assert len(out) == 1
    assert out[0]["property"] == "data_protection_law"
    assert out[0]["qualifiers"] == {"stage": "in_force"}
