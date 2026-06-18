"""Gemini CLI output handling: the `-o json` wrapper isolates the model's final
text in a `response` field, dropping agentic tool-call artifacts (e.g.
update_topic) and control tokens that leak into plain text-mode stdout.
"""
import json

from open_deep_research.claude_agent_chat import _gemini_response_text


def test_extracts_response_field_from_json_wrapper():
    raw = json.dumps({
        "session_id": "abc",
        "response": "The foundational digital identity scheme of Estonia is missing.",
        "stats": {"tools": {"totalCalls": 0}},
    })
    assert _gemini_response_text(raw) == (
        "The foundational digital identity scheme of Estonia is missing."
    )


def test_response_field_is_clean_even_when_text_mode_would_leak():
    # In `-o text` mode gemini-2.5-flash sometimes prepends a tool-call artifact
    # (`update_topic(...)`/<ctrl46>) to the answer. With `-o json` that chatter is
    # excluded from `response`, so extraction yields only the clean answer.
    raw = json.dumps({"response": "Clean answer.", "stats": {}})
    assert "update_topic" not in _gemini_response_text(raw)
    assert _gemini_response_text(raw) == "Clean answer."


def test_non_json_falls_back_to_raw():
    # If the CLI ever emits non-JSON (format drift), don't lose the output.
    assert _gemini_response_text("plain text answer") == "plain text answer"


def test_json_without_response_key_falls_back_to_raw():
    raw = json.dumps({"session_id": "x", "stats": {}})
    assert _gemini_response_text(raw) == raw
