from open_deep_research.factbase import entities


def test_resolves_canonical_and_common_aliases():
    r = entities.CountryResolver()
    assert r.resolve("France") == "FRA"
    assert r.resolve("Türkiye") == "TUR"
    assert r.resolve("Turkey") == "TUR"
    assert r.resolve("Côte d'Ivoire") == "CIV"


def test_miss_returns_none_never_guesses():
    assert entities.CountryResolver().resolve("Atlantis") is None


# --- resolve_in_text: pull a country out of a descriptive subject phrase --------------
# Regression: answer_from_facts passed the full subject phrase ("Estonia's digital identity
# scheme") to CountryResolver.resolve(), which is exact-match only -> None -> facts (stored
# under EST) were never retrieved and every property rendered "missing".
from open_deep_research.factbase.entities import CountryResolver


def test_resolve_in_text_extracts_country_from_subject_phrase():
    r = CountryResolver()
    assert r.resolve_in_text("Estonia's digital identity scheme") == "EST"


def test_resolve_in_text_prefers_multiword_country_names():
    r = CountryResolver()
    # Longest-match-first so "South Korea" wins over a bare "Korea" token.
    assert r.resolve_in_text("South Korea's national eID and its coverage") == "KOR"


def test_resolve_in_text_no_substring_false_positive():
    r = CountryResolver()
    # "Romania" normalises to a string containing "oman"; whole-token matching must NOT
    # mis-resolve it to Oman.
    assert r.resolve_in_text("Romania's digital ID programme") == "ROU"


def test_resolve_in_text_returns_none_when_no_country():
    r = CountryResolver()
    assert r.resolve_in_text("the digital identity landscape overview") is None


def test_resolve_in_text_still_handles_a_clean_country_name():
    r = CountryResolver()
    assert r.resolve_in_text("Estonia") == "EST"
