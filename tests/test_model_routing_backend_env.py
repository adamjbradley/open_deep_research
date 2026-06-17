from open_deep_research.model_routing import apply_backend_env, routing_from_dict

_R = routing_from_dict({
    "version": "1", "active_preset": "gemini",
    "backends": {"gemini": {"cli_bin": "gemini", "cli_args": [], "trust_workspace": True},
                 "codex": {"cli_bin": "codex", "sandbox": "read-only"}},
    "presets": {"gemini": {"roles": {"researcher": "gemini:gemini-2.5-flash"}}},
})


def test_apply_populates_gemini_env(monkeypatch):
    for k in ("GEMINI_CLI_BIN", "GEMINI_CLI_TRUST_WORKSPACE", "GEMINI_CLI_ARGS",
              "GEMINI_SEARCH_ARGS", "CODEX_CLI_BIN", "CODEX_SANDBOX"):
        monkeypatch.delenv(k, raising=False)
    apply_backend_env(_R)
    import os
    assert os.environ["GEMINI_CLI_BIN"] == "gemini"
    assert os.environ["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert os.environ["GEMINI_CLI_ARGS"] == ""
    assert os.environ["CODEX_SANDBOX"] == "read-only"


def test_explicit_env_is_not_overridden(monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_BIN", "agy")  # operator override
    apply_backend_env(_R)
    import os
    assert os.environ["GEMINI_CLI_BIN"] == "agy"  # setdefault: explicit wins
