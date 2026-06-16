import asyncio

from open_deep_research.factbase import registry_provision as rp
from open_deep_research.factbase.registry_scaffold import RegistryProposal


def test_existing_registry_is_reused(monkeypatch):
    # di_source_registry ships with the package -> reused, no scaffold, no commit.
    called = {"commit": 0}
    monkeypatch.setattr(rp, "git_commit_paths",
                        lambda paths, msg: called.__setitem__("commit", called["commit"] + 1))
    name = asyncio.run(rp.ensure_registry(
        registry_name="di_source_registry", domain_label="di", description="x",
        observed_domains=[], model_call=None, autocommit=True))
    assert name == "di_source_registry"
    assert called["commit"] == 0


def test_missing_registry_is_scaffolded_and_committed(tmp_path, monkeypatch):
    commits = []
    monkeypatch.setattr(rp, "git_commit_paths", lambda paths, msg: commits.append((paths, msg)))
    monkeypatch.setattr(rp, "_profiles_dir", lambda: str(tmp_path))

    async def fake_call(prompt):
        return RegistryProposal(sources=[{"domain": "cbn.gov.ng", "tier": "authoritative",
                                          "flags": ["primary"], "rationale": "issuer",
                                          "confidence": "high"}])

    name = asyncio.run(rp.ensure_registry(
        registry_name=None, domain_label="cbdc", description="central bank digital currency",
        observed_domains=["cbn.gov.ng"], model_call=fake_call, autocommit=True))
    assert name == "cbdc_source_registry"
    assert (tmp_path / "cbdc_source_registry.yaml").is_file()
    assert (tmp_path / "cbdc_source_registry.draft.yaml").is_file()
    assert len(commits) == 1
