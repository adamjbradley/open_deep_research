import asyncio
import open_deep_research.nodes.profiles as profiles


def test_whole_profile_returns_all(monkeypatch):
    prof = type("P", (), {"properties": [type("X", (), {"name": "a", "value_kind": "str"})(),
                                          type("X", (), {"name": "b", "value_kind": "str"})()]})()
    monkeypatch.setattr("open_deep_research.factbase.profile.load", lambda n: prof)
    cfg = type("C", (), {"whole_profile_mode": True, "facts_first_mode": False})()
    out = asyncio.run(profiles.resolve_run_target_properties("q", "country_digital_identity", cfg, {}))
    assert out == ["a", "b"]


def test_facts_first_delegates(monkeypatch):
    prof = type("P", (), {"properties": [type("X", (), {"name": "a", "value_kind": "str"})()]})()
    monkeypatch.setattr("open_deep_research.factbase.profile.load", lambda n: prof)
    async def fake_resolve(question, p, c, cfg): return ["a"]
    monkeypatch.setattr(profiles, "resolve_target_properties", fake_resolve)
    cfg = type("C", (), {"whole_profile_mode": False, "facts_first_mode": True})()
    out = asyncio.run(profiles.resolve_run_target_properties("q", "p", cfg, {}))
    assert out == ["a"]
