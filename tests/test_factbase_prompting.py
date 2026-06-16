from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.prompting import build_extraction_prompt, compile_property_catalog

PROF = profile_from_dict({
    "entity_type": "country", "version": "1",
    "properties": [
        {"name": "scheme_status", "kind": "enum", "description": "maturity",
         "identity_qualifiers": ["basis"], "required_qualifiers": ["basis"],
         "qualifier_enums": {"basis": ["de_jure", "de_facto"]},
         "value_enum": ["operational", "mandatory"]},
        {"name": "scheme_name", "kind": "name", "description": "the scheme"},
    ],
})


def test_catalog_includes_kind_description_enums_qualifiers():
    cat = compile_property_catalog(PROF)
    assert "scheme_status" in cat and "(enum)" in cat
    assert "maturity" in cat
    assert "operational" in cat and "mandatory" in cat
    assert "basis" in cat


def test_catalog_respects_target_properties():
    cat = compile_property_catalog(PROF, target_properties=["scheme_name"])
    assert "scheme_name" in cat
    assert "scheme_status" not in cat


def test_compiled_prompt_uses_entity_type_and_catalog():
    p = build_extraction_prompt(PROF, None, "SRC TEXT", compiled=True)
    assert "COUNTRY" in p
    assert "scheme_status" in p and "operational" in p
    assert "SRC TEXT" in p
    assert "evidence_span" in p


def test_names_only_prompt_when_not_compiled():
    p = build_extraction_prompt(PROF, None, "SRC", compiled=False)
    assert "scheme_status" in p
    assert "operational" not in p
    assert "Only use these property names" in p


def test_compile_flag_default_on():
    from open_deep_research.configuration import Configuration
    assert Configuration().compile_extraction_prompt is True
