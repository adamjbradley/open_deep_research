"""Ensure a usable source registry exists for a domain; scaffold+commit one if not.

Resolution order: (1) an explicit registry_name that loads non-empty -> reuse;
(2) else derive '<domain_label>_source_registry' and reuse if it loads; (3) else
scaffold it from the description (+ observed domains), write the usable .yaml plus an
audit .draft.yaml, git-commit both, and use it. Lets corroborated facts promote.
"""
from __future__ import annotations

import os
import subprocess

from .registry import SourceRegistry
from .registry_scaffold import induce_registry, render_registry_draft_yaml, render_registry_yaml


def _profiles_dir() -> str:
    """Directory where registry/profile YAML lives (monkeypatchable in tests)."""
    return os.path.join(os.path.dirname(__file__), "profiles")


def git_commit_paths(paths: list[str], msg: str) -> None:
    """Stage + commit specific paths. Non-fatal on failure (warn, don't abort paid research)."""
    try:
        subprocess.run(["git", "add", *paths], check=True)
        subprocess.run(["git", "commit", "-m", msg], check=True)
    except Exception as e:  # noqa: BLE001 - a commit failure must not crash a batch mid-run
        print(f"registry auto-commit failed (non-fatal): {e}")  # noqa: T201


def _loads_nonempty(name: str) -> bool:
    try:
        reg = SourceRegistry.load(name)
        return bool(getattr(reg, "_entries", None))
    except Exception:  # noqa: BLE001
        return False


async def ensure_registry(*, registry_name, domain_label, description, observed_domains,
                          model_call, autocommit: bool) -> str:
    """Return a usable registry name, scaffolding+committing one if none exists.

    Returns the resolved/derived registry name. If no registry exists and no model_call
    is available, returns the would-be name WITHOUT creating it (facts stay provisional).
    """
    if registry_name and _loads_nonempty(registry_name):
        return registry_name
    derived = f"{domain_label}_source_registry"
    if _loads_nonempty(derived):
        return derived
    if model_call is None:
        return registry_name or derived
    proposal = await induce_registry(domain_label, description, observed_domains, model_call)
    out_yaml = os.path.join(_profiles_dir(), f"{derived}.yaml")
    out_draft = os.path.join(_profiles_dir(), f"{derived}.draft.yaml")
    with open(out_yaml, "w", encoding="utf-8") as fh:
        fh.write(render_registry_yaml(proposal))
    with open(out_draft, "w", encoding="utf-8") as fh:
        fh.write(render_registry_draft_yaml(proposal))
    if autocommit:
        git_commit_paths([out_yaml, out_draft], f"feat(factbase): auto-scaffold {derived}")
    return derived
