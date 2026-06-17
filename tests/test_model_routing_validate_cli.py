import asyncio
import json

from open_deep_research.factbase.dossier import run


def test_validate_accepts_good_routing(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "g",
        "presets": {"g": {"roles": {"researcher": "gemini:gemini-2.5-flash"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    out = asyncio.run(run(["validate"]))
    assert "model_routing.json" in out and "INVALID" not in out


def test_validate_rejects_bad_routing(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "missing",
        "presets": {"g": {"roles": {"researcher": "gemini:x"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    out = asyncio.run(run(["validate"]))
    assert "INVALID" in out and "model_routing" in out
