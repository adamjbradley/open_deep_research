from open_deep_research.factbase import entities


def test_resolves_canonical_and_common_aliases():
    r = entities.CountryResolver()
    assert r.resolve("France") == "FRA"
    assert r.resolve("Türkiye") == "TUR"
    assert r.resolve("Turkey") == "TUR"
    assert r.resolve("Côte d'Ivoire") == "CIV"


def test_miss_returns_none_never_guesses():
    assert entities.CountryResolver().resolve("Atlantis") is None
