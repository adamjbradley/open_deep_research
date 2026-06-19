import json
from importlib.resources import files


def _head(spec):
    """A role spec is now a string OR a failover chain (list, primary first)."""
    return spec[0] if isinstance(spec, list) else spec


def test_bundled_routing_is_importable_and_valid_json():
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["active_preset"] == "claude"  # all-Claude default (gemini CLI deprecated -> agy)
    assert {"balanced", "gemini", "claude"} <= set(data["presets"])
    # primaries (chain heads): pure presets unchanged; balanced runs researcher on gemini.
    assert _head(data["presets"]["gemini"]["roles"]["researcher"]) == "gemini:gemini-2.5-flash"
    assert _head(data["presets"]["claude"]["roles"]["supervisor"]) == "claude-opus-4-8"


def test_balanced_preset_is_claude_reasoning_gemini_researcher():
    """The active 'balanced' preset keeps Claude on the reasoning seams but runs the
    researcher (throughput) on gemini-2.5-flash, with a cross-backend backup either way."""
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    roles = json.loads(text)["presets"]["balanced"]["roles"]
    assert _head(roles["researcher"]) == "gemini:gemini-2.5-flash"      # throughput king
    assert "claude" in roles["researcher"][1]                           # Claude quality backup
    assert _head(roles["supervisor"]) == "claude-opus-4-8"              # reasoning seam stays Claude
    assert _head(roles["final_report"]) == "claude-opus-4-8"


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
