import json
from importlib.resources import files


def test_bundled_routing_is_importable_and_valid_json():
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["active_preset"] == "gemini"
    assert "gemini" in data["presets"] and "claude" in data["presets"]
    assert data["presets"]["gemini"]["roles"]["researcher"] == "gemini:gemini-2.5-flash"
