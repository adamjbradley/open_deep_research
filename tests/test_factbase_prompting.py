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


def test_enum_descriptions_populated_and_rendered():
    p = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "s", "kind": "enum", "value_enum": [
            {"value": "operational", "description": "issuing at scale"},
            "mandatory"]}]})
    pd = p.property("s")
    assert pd.value_enum_descriptions == {"operational": "issuing at scale"}
    cat = compile_property_catalog(p)
    assert "operational (issuing at scale)" in cat
    assert "mandatory" in cat  # value without a description still listed


def test_plain_enum_has_no_descriptions():
    p = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "s", "kind": "enum", "value_enum": ["a", "b"]}]})
    assert p.property("s").value_enum_descriptions == {}


def _cat(**flags):
    base = {"name": "b", "kind": "enum", "description": "modality",
            "value_enum": ["photo", "iris"]}
    base.update(flags)
    prof = profile_from_dict({"entity_type": "c", "properties": [base]})
    return compile_property_catalog(prof)


def test_multi_closed_line_says_select_all():
    cat = _cat(multi=True)
    assert "select all that apply" in cat
    assert "allowed values" in cat


def test_multi_open_line_says_others_verbatim_and_known_values():
    cat = _cat(multi=True, open=True)
    assert "select all that apply" in cat
    assert "list others verbatim" in cat
    assert "known values" in cat and "allowed values" not in cat


def test_single_open_line_says_literal_and_known_values():
    cat = _cat(open=True)
    assert "give the literal" in cat
    assert "known values" in cat


def test_single_closed_line_unchanged():
    cat = _cat()
    assert "(enum)" in cat and "allowed values" in cat


def test_shipped_di_profile_constrains_foundational_scheme_extraction():
    """The foundational_id_scheme description must steer the extractor to a SINGLE official
    scheme and away from adjacent e-services (regression: over-extraction of 'e-residency',
    'bank' as the foundational scheme). The description compiles straight into the prompt."""
    from open_deep_research.factbase import profile as profmod
    prof = profmod.load("country_digital_identity")
    cat = compile_property_catalog(prof, target_properties=["foundational_id_scheme"])
    low = cat.lower()
    assert "single" in low                      # only the one official scheme
    assert "e-residency" in low or "e residency" in low  # explicit don't-extract example


def test_catalog_includes_narrative_guidance():
    prof = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "scheme", "kind": "name",
         "narrative": {"required": True, "guidance": "Explain enrolment and caveats."}},
    ]})
    cat = compile_property_catalog(prof, ["scheme"])
    assert "narrative" in cat.lower()
    assert "enrolment" in cat


def test_extraction_prompt_requests_flat_qualifier_tokens_and_json_array():
    prof = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "cov", "kind": "percentage", "identity_qualifiers": ["population_basis"],
         "qualifier_enums": {"population_basis": ["total_pop"]}},
    ]})
    p = build_extraction_prompt(prof, ["cov"], "Estonia: 99% of total population.", compiled=True)
    low = p.lower()
    assert "json array" in low
    assert "evidence_span" in p
    assert "qualifiers" in low and "list" in low      # flat list, not an object
    assert "do not" in low and "object" in low        # explicit: not a nested object
