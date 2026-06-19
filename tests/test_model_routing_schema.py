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


def test_nvidia_model_prefix_accepted():
    # NVIDIA's OpenAI-compatible backend is addressed by an ``nvidia:`` prefix.
    ok = {**_VALID, "presets": {"gemini": {"roles": {
        "researcher": "nvidia:nvidia/nemotron-3-ultra-550b-a55b"}}}}
    r = routing_from_dict(ok)
    assert r.presets["gemini"].roles["researcher"].startswith("nvidia:")


def test_unknown_step_override_key_rejected():
    bad = {**_VALID, "presets": {"gemini": {"roles": {}, "step_overrides": {"no_such_step": "claude:sonnet"}}}}
    with pytest.raises(ValueError):
        routing_from_dict(bad)


def test_load_routing_reads_bundled_default():
    r = load_routing()  # no file in cwd / no env -> bundled
    assert "gemini" in r.presets


def test_nvidia_extract_facts_leads_with_strong_extractor():
    from open_deep_research.model_routing import load_routing
    chain = load_routing().presets["nvidia"].step_overrides["extract_facts"]
    assert chain[0] == "agy:gemini-3.1-pro-high"   # strong recall, not the throttled minimax-m3


def test_known_search_accepts_exa_hybrid():
    r = routing_from_dict({"version": "1", "active_preset": "p",
        "presets": {"p": {"roles": {"researcher": "claude:haiku"}, "search": "tavily_exa"}}})
    assert r.presets["p"].search == "tavily_exa"

    r2 = routing_from_dict({"version": "1", "active_preset": "p",
        "presets": {"p": {"roles": {"researcher": "claude:haiku"}, "search": "exa"}}})
    assert r2.presets["p"].search == "exa"
