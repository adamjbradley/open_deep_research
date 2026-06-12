from open_deep_research.factbase import registry
def test_known_domain_resolves_to_tier():
    r = registry.SourceRegistry.load("di_source_registry")
    assert r.tier("https://id4d.worldbank.org/data") == "authoritative"
    assert r.tier("https://some-random-blog.example/post") == "unvetted"
def test_meets_bar_orders_tiers():
    r = registry.SourceRegistry.load("di_source_registry")
    assert r.meets_bar("https://id4d.worldbank.org/x", "reputable") is True
    assert r.meets_bar("https://some-random-blog.example/p", "reputable") is False
def test_modeled_flag_surfaced():
    r = registry.SourceRegistry.load("di_source_registry")
    assert "modeled" in r.flags("https://id4d.worldbank.org/data")
