import pytest
from open_deep_research.claude_agent_chat import to_agy_model


def test_to_agy_model_maps_known_slugs():
    assert to_agy_model("gemini-3.5-flash-high") == "Gemini 3.5 Flash (High)"
    assert to_agy_model("gemini-3.1-pro-low") == "Gemini 3.1 Pro (Low)"
    assert to_agy_model("claude-opus-4.6") == "Claude Opus 4.6 (Thinking)"
    assert to_agy_model("gpt-oss-120b") == "GPT-OSS 120B (Medium)"


def test_to_agy_model_strips_agy_prefix():
    assert to_agy_model("agy:gemini-3.5-flash-medium") == "Gemini 3.5 Flash (Medium)"


def test_to_agy_model_unknown_slug_raises():
    with pytest.raises(ValueError):
        to_agy_model("gemini-2.5-flash")        # not an agy slug -> must NOT silently default
    with pytest.raises(ValueError):
        to_agy_model("")
