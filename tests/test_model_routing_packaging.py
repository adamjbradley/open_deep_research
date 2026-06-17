import json
from importlib.resources import files


def _head(spec):
    """A role spec is now a string OR a failover chain (list, primary first)."""
    return spec[0] if isinstance(spec, list) else spec


def test_bundled_routing_is_importable_and_valid_json():
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["active_preset"] == "claude"  # benchmark recommendation (claude.feedback)
    assert "gemini" in data["presets"] and "claude" in data["presets"]
    # primaries (chain heads) are unchanged; chains add cross-backend backups.
    assert _head(data["presets"]["gemini"]["roles"]["researcher"]) == "gemini:gemini-2.5-flash"
    assert _head(data["presets"]["claude"]["roles"]["supervisor"]) == "claude-opus-4-8"


def test_bundled_presets_ship_cross_backend_backups():
    """Each preset's roles now carry a failover chain whose head is the preset's own
    backend and which includes at least one cross-backend backup."""
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    data = json.loads(text)
    for name, marker in (("gemini", "gemini:"), ("claude", "claude"), ("codex", "codex:")):
        for role, spec in data["presets"][name]["roles"].items():
            assert isinstance(spec, list) and len(spec) >= 2, f"{name}.{role} has no backup: {spec}"
            assert marker in _head(spec), f"{name}.{role} head not {marker!r}: {spec}"
            # a backup on a different backend (the chain isn't all one provider)
            assert any(marker not in m for m in spec[1:]), f"{name}.{role} backups not cross-backend: {spec}"
