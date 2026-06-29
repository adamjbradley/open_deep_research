# tests/test_extraction_prompt.py
from open_deep_research.factbase.prompting import build_extraction_prompt, oversized_catalog_warning
from open_deep_research.factbase import profile as fbprofile
from open_deep_research.factbase.profile_schema import profile_from_dict

def test_source_cap_includes_text_past_8000():
    prof = fbprofile.load("country_digital_identity")
    marker = "UNIQUE_FACT_MARKER_12345"
    src = ("x" * 12000) + " " + marker
    prompt = build_extraction_prompt(prof, None, src, compiled=False)
    assert marker in prompt   # text at ~char 12000 must reach the model


def test_source_cap_admits_up_to_40000_chars():
    # Long legislative/statute sources carry substantive provisions deep in the document;
    # the cap was raised 24k -> 40k so more of them reaches extraction.
    prof = fbprofile.load("country_digital_identity")
    src = "Z" * 45000
    prompt = build_extraction_prompt(prof, None, src, compiled=False)
    assert prompt.count("Z") == 40000


def test_no_catalog_warning_for_lean_production_profile():
    # country_cbdc has the largest production catalog (~4.1k chars, 14 props) and is still lean.
    assert oversized_catalog_warning(fbprofile.load("country_cbdc")) is None


def test_catalog_warning_fires_for_oversized_profile():
    big = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": f"prop_{i}", "kind": "text", "description": "D" * 400} for i in range(40)
    ]})
    msg = oversized_catalog_warning(big)
    assert msg is not None
    assert "trimming the profile" in msg
    assert "40 propert" in msg  # reports the property count, not the (source-driven) prompt size


def test_compiled_prompt_instructs_boolean_value():
    # boolean-kind properties (e.g. data_protection_law) need an explicit value rule: without it
    # the model emits NO fact for them (confirmed against the live extraction model). The rule
    # must tell the model to emit "true"/"false".
    prof = fbprofile.load("country_digital_identity")  # has the boolean data_protection_law
    prompt = build_extraction_prompt(prof, ["data_protection_law"], "src", compiled=True)
    assert '"true"' in prompt and '"false"' in prompt


def test_catalog_marks_required_qualifiers():
    from open_deep_research.factbase.prompting import compile_property_catalog
    prof = fbprofile.load("country_digital_identity")  # data_protection_law requires `stage`
    cat = compile_property_catalog(prof, ["data_protection_law"])
    assert "stage=" in cat
    assert "(REQUIRED)" in cat                      # stage is marked required
    # a non-required qualifier on the same property is NOT marked
    assert "scope=" in cat and "scope=['comprehensive', 'sectoral'] (REQUIRED)" not in cat
