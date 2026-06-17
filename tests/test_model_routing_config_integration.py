from open_deep_research.configuration import Configuration


def test_config_uses_routing_preset_for_roles(monkeypatch):
    for k in ("RESEARCHER_MODEL", "SUPERVISOR_MODEL", "MODEL_ROUTING_FILE", "MODEL_ROUTING_PRESET"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    # active preset = balanced (Claude reasoning seams + Gemini researcher/throughput)
    assert c.researcher_model == "gemini:gemini-2.5-flash"   # researcher on the throughput king
    assert c.supervisor_model == "claude-opus-4-8"           # reasoning seam stays Claude


def test_env_overrides_routing(monkeypatch):
    for k in ("MODEL_ROUTING_FILE", "MODEL_ROUTING_PRESET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCHER_MODEL", "claude:sonnet")
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "claude:sonnet"        # env wins
    assert c.supervisor_model == "claude-opus-4-8"      # others from the active claude preset


def test_preset_switch(monkeypatch):
    for k in ("RESEARCHER_MODEL", "MODEL_ROUTING_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "claude")
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "claude-opus-4-6"


def test_model_for_step_override(monkeypatch, tmp_path):
    import json
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "g",
        "presets": {"g": {"roles": {"researcher": "gemini:gemini-2.5-flash"},
                          "step_overrides": {"extract_facts": "claude:sonnet"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    monkeypatch.delenv("RESEARCHER_MODEL", raising=False)
    monkeypatch.delenv("MODEL_ROUTING_PRESET", raising=False)
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "gemini:gemini-2.5-flash"
    assert c.model_for("extract_facts", "researcher") == "claude:sonnet"


def test_configurable_beats_preset(monkeypatch):
    for k in ("RESEARCHER_MODEL", "MODEL_ROUTING_FILE", "MODEL_ROUTING_PRESET"):
        monkeypatch.delenv(k, raising=False)
    # active preset has its own researcher model; configurable must still win
    c = Configuration.from_runnable_config({"configurable": {"researcher_model": "codex:gpt-5.5"}})
    assert c.researcher_model == "codex:gpt-5.5"


def test_extract_facts_model_uses_model_for(monkeypatch, tmp_path):
    import json
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "g",
        "presets": {"g": {"roles": {"researcher": "gemini:gemini-2.5-flash"},
                          "step_overrides": {"extract_facts": "claude:sonnet"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    monkeypatch.delenv("RESEARCHER_MODEL", raising=False)
    monkeypatch.delenv("MODEL_ROUTING_PRESET", raising=False)
    c = Configuration.from_runnable_config({})
    # the extraction seam must resolve to the step override, not the researcher role
    assert c.researcher_model == "gemini:gemini-2.5-flash"
    assert c.model_for("extract_facts", "researcher") == "claude:sonnet"
