from open_deep_research.factbase.entities import CountryResolver


def test_resolves_iso_names_and_aliases():
    r = CountryResolver()
    assert r.resolve("Bahamas") == "BHS"        # the blocker case
    assert r.resolve("Nigeria") == "NGA"        # original 20 still work
    assert r.resolve("United Kingdom") == "GBR"
    assert r.resolve("UK") == "GBR"             # alias
    assert r.resolve("Türkiye") == "TUR"        # diacritics + endonym
    assert r.resolve("south korea") == "KOR"    # case-insensitive


def test_unresolved_returns_none():
    r = CountryResolver()
    assert r.resolve("Atlantis") is None
    assert r.resolve("") is None


def test_instance_name_reverse_lookup():
    r = CountryResolver()
    assert r.instance_name("BHS") == "Bahamas"  # primary name for matrix labels
    assert r.instance_name("ZZZ") == "ZZZ"      # unknown key -> echo the key
