import asyncio
import pytest
from open_deep_research.claude_agent_chat import build_chat_model, to_agy_model, AgyCLIChat


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


def test_agy_prefix_builds_agy_backend_with_display_name():
    m = build_chat_model("agy:gemini-3.1-pro-high")
    assert isinstance(m, AgyCLIChat)
    assert m.model == "Gemini 3.1 Pro (High)"     # mapped to the display name


def test_agy_command_has_no_o_json_and_no_skip_permissions_by_default(monkeypatch):
    m = build_chat_model("agy:gemini-3.5-flash-high")
    captured = {}
    async def fake_invoke(cmd, stdin=None):
        captured["cmd"] = cmd; captured["stdin"] = stdin
        return '[{"ok": true}]'
    monkeypatch.setattr(m, "_invoke", fake_invoke)
    asyncio.run(m._backend_generate("sys", "hello", None))
    assert captured["cmd"][:3] == ["agy", "--model", "Gemini 3.5 Flash (High)"]
    assert "--dangerously-skip-permissions" not in captured["cmd"]  # secure default
    assert "-o" not in captured["cmd"] and "json" not in captured["cmd"]
    assert captured["stdin"] is not None      # prompt via stdin


def test_agy_subprocess_env_scrubs_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    m = build_chat_model("agy:gemini-3.5-flash-high")
    env = m._subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env and "TAVILY_API_KEY" not in env
