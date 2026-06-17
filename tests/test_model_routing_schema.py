import pytest

from open_deep_research.model_routing import RoutingConfig, load_routing, routing_from_dict

_VALID = {
    "version": "1", "active_preset": "gemini",
    "backends": {"gemini": {"cli_bin": "gemini", "trust_workspace": True}},
    "presets": {"gemini": {"roles": {"researcher": "gemini:gemini-2.5-flash"},
                           "search": "tavily", "step_overrides": {"extract_facts": "claude:sonnet"}}},
}


def test_valid_routing_parses():
    r = routing_from_dict(_VALID)
    assert isinstance(r, RoutingConfig)
    assert r.active_preset == "gemini"
    assert r.presets["gemini"].roles["researcher"] == "gemini:gemini-2.5-flash"


def test_active_preset_must_exist():
    bad = {**_VALID, "active_preset": "nope"}
    with pytest.raises(ValueError):
        routing_from_dict(bad)


def test_unknown_role_rejected():
    bad = {**_VALID, "presets": {"gemini": {"roles": {"bogus_role": "gemini:x"}}}}
    with pytest.raises(ValueError):
        routing_from_dict(bad)


def test_unknown_model_prefix_rejected():
    bad = {**_VALID, "presets": {"gemini": {"roles": {"researcher": "mistral:big"}}}}
    with pytest.raises(ValueError):
        routing_from_dict(bad)


def test_unknown_step_override_key_rejected():
    bad = {**_VALID, "presets": {"gemini": {"roles": {}, "step_overrides": {"no_such_step": "claude:sonnet"}}}}
    with pytest.raises(ValueError):
        routing_from_dict(bad)


def test_load_routing_reads_bundled_default():
    r = load_routing()  # no file in cwd / no env -> bundled
    assert "gemini" in r.presets
