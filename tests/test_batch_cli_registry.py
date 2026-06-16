"""CLI registry auto-provision wiring for `dossier batch`.

Verifies that the batch CLI (a) auto-provisions a source registry by default and threads
its name into per-country runs so facts can promote, (b) skips provisioning under
--no-registry-autoprovision, and (c) that default_run_one forwards registry_name into the
graph config. No real LLM/graph/git is invoked (all external seams are monkeypatched).
"""
import asyncio
import functools
from importlib.resources import files

import pytest

if not files("open_deep_research.factbase.profiles").joinpath("country_cbdc.yaml").is_file():
    pytest.skip("country_cbdc profile not present", allow_module_level=True)


class _FakeRunner:
    """Captures the run_one passed in; never invokes the real graph."""
    last = {}

    def __init__(self, *, profile_name, db_path, concurrency, run_one, profile_hash="", list_spec=""):
        _FakeRunner.last = {"run_one": run_one}  # reset (not mutate) so tests don't see stale state

    async def run(self, names):
        return {"batch_id": "b_x", "summary": {"done": len(names)}, "unresolved": [], "resolved": []}


def _patch_runner_and_model(monkeypatch):
    from open_deep_research.factbase import batch as batchmod
    from open_deep_research.factbase import dossier as dossiermod
    monkeypatch.setattr(batchmod, "BatchRunner", _FakeRunner)
    # the real model factory imports the graph model; stub it (ensure_registry is also stubbed)
    monkeypatch.setattr(dossiermod, "_registry_scaffold_model_call", lambda: None)


def test_batch_cli_autoprovisions_and_threads_registry(tmp_path, monkeypatch):
    from open_deep_research.factbase import registry_provision as rp
    from open_deep_research.factbase.dossier import run

    captured = {}

    async def fake_ensure(*, registry_name, domain_label, description, observed_domains,
                          model_call, autocommit):
        captured["ensure_called"] = True
        captured["domain_label"] = domain_label
        return "country_cbdc_source_registry"

    monkeypatch.setattr(rp, "ensure_registry", fake_ensure)
    _patch_runner_and_model(monkeypatch)

    out = asyncio.run(run(["batch", "--profile", "country_cbdc", "--countries", "Nigeria"],
                          db_path=str(tmp_path / "c.db")))
    assert captured.get("ensure_called") is True
    assert captured["domain_label"] == "country_cbdc"
    assert "country_cbdc_source_registry" in out          # reported in the summary line
    run_one = _FakeRunner.last["run_one"]
    assert isinstance(run_one, functools.partial)
    assert run_one.keywords.get("registry_name") == "country_cbdc_source_registry"


def test_batch_cli_no_autoprovision_skips_ensure(tmp_path, monkeypatch):
    from open_deep_research.factbase import registry_provision as rp
    from open_deep_research.factbase.dossier import run

    captured = {}

    async def fake_ensure(**kw):
        captured["ensure_called"] = True
        return "should_not_be_used"

    monkeypatch.setattr(rp, "ensure_registry", fake_ensure)
    _patch_runner_and_model(monkeypatch)

    out = asyncio.run(run(["batch", "--profile", "country_cbdc", "--countries", "Nigeria",
                           "--no-registry-autoprovision"], db_path=str(tmp_path / "n.db")))
    assert "ensure_called" not in captured
    run_one = _FakeRunner.last["run_one"]
    assert run_one.keywords.get("registry_name") == ""    # nothing threaded
    assert "registry:" not in out                         # the "| registry: <name>" note is absent


def test_default_run_one_passes_registry_name(monkeypatch):
    import open_deep_research.deep_researcher as dr
    from open_deep_research.factbase.batch import default_run_one

    captured = {}

    async def fake_ainvoke(state, config):
        captured["config"] = config
        return {"report_id": 7}

    monkeypatch.setattr(dr.deep_researcher, "ainvoke", fake_ainvoke)
    rid = asyncio.run(default_run_one("Nigeria", "NGA", profile_name="country_cbdc",
                                      db_path=":memory:", registry_name="country_cbdc_source_registry"))
    assert rid == "7"
    assert captured["config"]["configurable"]["registry_name"] == "country_cbdc_source_registry"


def test_default_run_one_omits_registry_when_empty(monkeypatch):
    import open_deep_research.deep_researcher as dr
    from open_deep_research.factbase.batch import default_run_one

    captured = {}

    async def fake_ainvoke(state, config):
        captured["config"] = config
        return {"report_id": 1}

    monkeypatch.setattr(dr.deep_researcher, "ainvoke", fake_ainvoke)
    asyncio.run(default_run_one("Nigeria", "NGA", profile_name="country_cbdc", db_path=":memory:"))
    assert "registry_name" not in captured["config"]["configurable"]
