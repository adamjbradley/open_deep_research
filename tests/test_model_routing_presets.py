"""Backend coverage for routing presets + a distinct 'read the existing config file' test type.

Two concerns:
  1. Every shipped preset (claude/gemini/codex) resolves all roles to its own backend, end to
     end through Configuration.from_runnable_config -- so claude, gemini AND codex stay covered.
  2. The EXISTING, shipped model_routing.json (and an on-disk file) is read+validated as-is, not
     via a synthetic fixture -- a contract test on the real config the system actually loads.
"""
import json
import os
from importlib.resources import files

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.model_routing import load_routing, routing_from_dict

ROLE_FIELDS = ["supervisor_model", "researcher_model", "summarization_model",
               "compression_model", "final_report_model"]

# (preset name, substring every resolved role model must contain to prove the backend)
PRESET_BACKEND = [("claude", "claude"), ("gemini", "gemini:"), ("codex", "codex:")]


def _clear(monkeypatch):
    for k in ("MODEL_ROUTING_FILE", "MODEL_ROUTING_PRESET", "SUPERVISOR_MODEL",
              "RESEARCHER_MODEL", "SUMMARIZATION_MODEL", "COMPRESSION_MODEL",
              "FINAL_REPORT_MODEL"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("preset,marker", PRESET_BACKEND)
def test_each_preset_resolves_to_its_backend(monkeypatch, preset, marker):
    """claude / gemini / codex each route every role to their own backend (end to end)."""
    _clear(monkeypatch)
    monkeypatch.setenv("MODEL_ROUTING_PRESET", preset)
    c = Configuration.from_runnable_config({})
    for field in ROLE_FIELDS:
        model = getattr(c, field)
        assert marker in model, f"{preset} preset: {field}={model!r} lacks backend marker {marker!r}"


# --- distinct test type: read the EXISTING shipped config file as-is ---

def test_existing_shipped_config_valid_and_covers_all_backends():
    """The real bundled model_routing.json validates and ships a preset for each backend."""
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    r = routing_from_dict(json.loads(text))  # raises if the shipped file is invalid
    assert {"claude", "gemini", "codex"} <= set(r.presets)
    for name, expected in (("claude", "claude"), ("gemini", "gemini:"), ("codex", "codex:")):
        specs = list(r.presets[name].roles.values())
        assert specs, f"{name} preset has no roles"
        # a spec is a string OR a failover chain (list, primary first); the PRIMARY
        # (head) must be the preset's own backend.
        heads = [s[0] if isinstance(s, list) else s for s in specs]
        assert all(expected in h for h in heads), f"{name} preset primaries not all {expected!r}: {heads}"


def test_load_routing_reads_existing_on_disk_file(monkeypatch, tmp_path):
    """An EXISTING on-disk model_routing.json (via MODEL_ROUTING_FILE) is read + validated as-is."""
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "codex",
        "presets": {"codex": {"roles": {"researcher": "codex:gpt-5.5"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    r = load_routing()
    assert r.active_preset == "codex"
    assert r.presets["codex"].roles["researcher"] == "codex:gpt-5.5"


def test_agy_preset_resolves(monkeypatch):
    """The agy preset resolves all roles to agy backend with Claude backups."""
    _clear(monkeypatch)
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "agy")
    from open_deep_research.model_routing import load_routing, model_chain
    r = load_routing()
    assert "agy" in r.presets
    assert model_chain("researcher", routing=r)[0].startswith("agy:")
    assert model_chain("supervisor", routing=r)[0].startswith("agy:")
    # extract_facts step override present and agy-primary
    assert model_chain("researcher", routing=r, step="extract_facts")[0].startswith("agy:")
