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
    from open_deep_research.claude_agent_chat import build_chat_model
    scrubbed = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
                "GOOGLE_GENAI_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY",
                "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "NVIDIA_API_KEY",
                "SUPABASE_KEY", "EXA_API_KEY")
    for k in scrubbed:
        monkeypatch.setenv(k, "secret-" + k)
    monkeypatch.setenv("SOME_NONSECRET_VAR", "keepme")
    m = build_chat_model("agy:gemini-3.5-flash-high")
    env = m._subprocess_env()
    for k in scrubbed:
        assert k not in env, f"{k} should be scrubbed"
    assert env.get("SOME_NONSECRET_VAR") == "keepme"   # non-secrets pass through
