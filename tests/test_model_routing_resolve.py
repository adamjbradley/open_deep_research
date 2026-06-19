from open_deep_research.model_routing import resolve_model, resolve_search, routing_from_dict

_R = routing_from_dict({
    "version": "1", "active_preset": "gemini",
    "backends": {"gemini": {"cli_bin": "gemini"}},
    "presets": {
        "gemini": {"roles": {"researcher": "gemini:gemini-2.5-flash",
                             "supervisor": "gemini:gemini-2.5-flash"},
                   "search": "tavily", "step_overrides": {"extract_facts": "claude:sonnet"}},
        "claude": {"roles": {"researcher": "claude:sonnet"}, "search": "tavily"},
    },
})


def test_role_from_active_preset():
    assert resolve_model("researcher", routing=_R, env_value=None,
                         configurable_value=None, code_default="x") == "gemini:gemini-2.5-flash"


def test_step_override_beats_role():
    assert resolve_model("researcher", step="extract_facts", routing=_R, env_value=None,
                         configurable_value=None, code_default="x") == "claude:sonnet"


def test_env_beats_everything():
    assert resolve_model("researcher", step="extract_facts", routing=_R,
                         env_value="codex:gpt-5.5", configurable_value=None,
                         code_default="x") == "codex:gpt-5.5"


def test_code_default_when_role_absent():
    assert resolve_model("compression", routing=_R, env_value=None,
                         configurable_value=None, code_default="claude:haiku") == "claude:haiku"


def test_configurable_beats_preset_role():
    # researcher IS in the preset; configurable must still win
    assert resolve_model("researcher", routing=_R, env_value=None,
                         configurable_value="claude:opus", code_default="x") == "claude:opus"


def test_preset_switch_via_env(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "claude")
    assert resolve_model("researcher", routing=_R, env_value=None,
                         configurable_value=None, code_default="x") == "claude:sonnet"


def test_resolve_search_role_then_env(monkeypatch):
    assert resolve_search(routing=_R, env_value=None, configurable_value=None, code_default="none") == "tavily"
    assert resolve_search(routing=_R, env_value="codex", configurable_value=None, code_default="none") == "codex"


def test_propose_extensions_step_routes_to_pro_in_gemini_preset(monkeypatch):
    # ScaffoldProposal/RegistryProposal are complex schemas -> the propose_extensions step
    # override routes them to gemini-2.5-pro (with a Claude backup) instead of flash.
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "gemini")
    from open_deep_research.model_routing import load_routing, model_chain, KNOWN_STEPS
    assert "propose_extensions" in KNOWN_STEPS
    chain = model_chain("summarization", routing=load_routing(), step="propose_extensions")
    assert chain[0] == "gemini:gemini-2.5-pro"
    assert "claude-haiku-4-5" in chain          # backup preserved


def test_dossier_scaffold_cfg_uses_pro_chain(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "gemini")
    from open_deep_research.factbase.dossier import _propose_extensions_cfg
    cfg = _propose_extensions_cfg()
    assert cfg["stage"] == "propose_extensions"
    assert cfg["model"] == "gemini:gemini-2.5-pro"
    assert cfg["model_chain"][0] == "gemini:gemini-2.5-pro"
