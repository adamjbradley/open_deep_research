# tests/test_extraction_prompt.py
from open_deep_research.factbase.prompting import build_extraction_prompt
from open_deep_research.factbase import profile as fbprofile

def test_source_cap_includes_text_past_8000():
    prof = fbprofile.load("country_digital_identity")
    marker = "UNIQUE_FACT_MARKER_12345"
    src = ("x" * 12000) + " " + marker
    prompt = build_extraction_prompt(prof, None, src, compiled=False)
    assert marker in prompt   # text at ~char 12000 must reach the model
