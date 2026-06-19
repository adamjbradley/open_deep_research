import pytest
from open_deep_research.model_routing import routing_from_dict
from open_deep_research.failover import AvailabilityTracker
from open_deep_research import preflight as pf

ROUTING = {
    "version": "1", "active_preset": "gemini",
    "presets": {"gemini": {"roles": {
        "supervisor": ["gemini:gemini-2.5-flash", "claude-opus-4-8"],
        "researcher": ["gemini:gemini-2.5-flash", "claude-opus-4-6"],
    }, "search": "tavily"}},
}

def test_primary_backends_are_chain_heads():
    r = routing_from_dict(ROUTING)
    assert pf.primary_backends(r.active()) == {"gemini"}

def test_warn_marks_unusable_primary_down(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "warn")
    monkeypatch.setattr(pf, "probe_backend", lambda b: False)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    unusable = pf.run_preflight(r, t)
    assert unusable == ["gemini"]
    assert t.is_backend_down("gemini") is True

def test_fail_raises(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "fail")
    monkeypatch.setattr(pf, "probe_backend", lambda b: False)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    with pytest.raises(pf.PreflightError):
        pf.run_preflight(r, t)

def test_off_skips(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "off")
    called = []
    monkeypatch.setattr(pf, "probe_backend", lambda b: called.append(b) or True)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    assert pf.run_preflight(r, t) == []
    assert called == []

def test_probe_is_memoized(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "_probe_uncached", lambda b: calls.append(b) or True)
    pf._probe_cache.clear()
    assert pf.probe_backend("gemini") is True
    assert pf.probe_backend("gemini") is True
    assert calls == ["gemini"]

def test_probe_agy_backend(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda b: "/bin/agy" if b == "agy" else None)
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    assert pf._probe_uncached("agy") is True
    monkeypatch.setattr(pf.shutil, "which", lambda b: None)
    assert pf._probe_uncached("agy") is False
