"""Model/search routing as data: a validated, dynamically-read model_routing.json.

Holds named presets, per-role models, per-step overrides, and per-backend settings.
Resolution order (highest wins): explicit env var > preset step_override > preset role >
code default. The graph and claude_agent_chat.py are unchanged: model roles are resolved
into Configuration fields, and backend settings are pushed into os.environ via setdefault.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel, model_validator

KNOWN_ROLES = {"supervisor", "researcher", "summarization", "compression",
               "final_report", "facts_answer_polish"}
KNOWN_STEPS = {"extract_facts"}  # expand as more call sites adopt model_for()
KNOWN_BACKENDS = {"claude", "gemini", "codex"}
KNOWN_PREFIXES = {"claude", "gemini", "google", "codex", "openai", "anthropic"}
KNOWN_SEARCH = {"claude", "gemini", "codex", "anthropic", "openai", "tavily", "none"}


def _check_model_string(value: str, where: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{where}: empty model string")
    if ":" in value:
        prefix = value.split(":", 1)[0].strip().lower()
        if prefix not in KNOWN_PREFIXES:
            raise ValueError(f"{where}: unknown backend prefix {prefix!r} in {value!r}")


class BackendSettings(BaseModel):
    """Per-backend CLI/runtime settings pushed into the environment for a backend."""

    cli_bin: Optional[str] = None
    cli_args: list[str] = []
    trust_workspace: Optional[bool] = None
    sandbox: Optional[str] = None
    subscription: Optional[bool] = None
    model_aliases: dict[str, str] = {}


class Preset(BaseModel):
    """A named backend bundle: per-role models, a search backend, and per-step overrides."""

    roles: dict[str, str] = {}
    search: Optional[str] = None
    step_overrides: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Preset:
        for role, model in self.roles.items():
            if role not in KNOWN_ROLES:
                raise ValueError(f"unknown role {role!r} (known: {sorted(KNOWN_ROLES)})")
            _check_model_string(model, f"roles.{role}")
        for step, model in self.step_overrides.items():
            if step not in KNOWN_STEPS:
                raise ValueError(f"unknown step_override {step!r} (known: {sorted(KNOWN_STEPS)})")
            _check_model_string(model, f"step_overrides.{step}")
        if self.search is not None and self.search not in KNOWN_SEARCH:
            raise ValueError(f"unknown search {self.search!r} (known: {sorted(KNOWN_SEARCH)})")
        return self


class RoutingConfig(BaseModel):
    """The whole routing file: version, active preset, backend settings, and presets."""

    version: str = "1"
    active_preset: str
    backends: dict[str, BackendSettings] = {}
    presets: dict[str, Preset]

    @model_validator(mode="after")
    def _check(self) -> RoutingConfig:
        if self.active_preset not in self.presets:
            raise ValueError(f"active_preset {self.active_preset!r} not in presets {sorted(self.presets)}")
        for name in self.backends:
            if name not in KNOWN_BACKENDS:
                raise ValueError(f"unknown backend {name!r} (known: {sorted(KNOWN_BACKENDS)})")
        return self

    def active(self) -> Preset:
        """Return the active preset (MODEL_ROUTING_PRESET env overrides active_preset)."""
        name = os.environ.get("MODEL_ROUTING_PRESET") or self.active_preset
        if name not in self.presets:
            raise ValueError(f"MODEL_ROUTING_PRESET {name!r} not in presets {sorted(self.presets)}")
        return self.presets[name]


def routing_from_dict(data: dict) -> RoutingConfig:
    """Validate a parsed routing dict and return the typed RoutingConfig (raises on invalid)."""
    return RoutingConfig.model_validate(data)


def _routing_path() -> str:
    env = os.environ.get("MODEL_ROUTING_FILE")
    if env:
        return env
    cwd = os.path.join(os.getcwd(), "model_routing.json")
    if os.path.isfile(cwd):
        return cwd
    return ""  # signal: use bundled


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime: float) -> RoutingConfig:
    if path:
        with open(path, encoding="utf-8") as fh:
            return routing_from_dict(json.load(fh))
    from importlib.resources import files
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    return routing_from_dict(json.loads(text))


def load_routing() -> RoutingConfig:
    """Load + validate the routing file (env file > ./model_routing.json > bundled).

    Memoized by (path, mtime) so an unchanged file isn't re-parsed but edits are picked
    up on the next run.
    """
    path = _routing_path()
    mtime = os.path.getmtime(path) if path else 0.0
    return _load_cached(path, mtime)


def resolve_model(role: str, *, routing: RoutingConfig | None = None, step: str | None = None,
                  env_value: str | None = None, configurable_value: str | None = None,
                  code_default: str | None = None) -> str | None:
    """Resolve a model string: env > configurable > step_override > role > code default."""
    if env_value:
        return env_value
    if configurable_value is not None:
        return configurable_value
    routing = routing or load_routing()
    preset = routing.active()
    if step and step in preset.step_overrides:
        return preset.step_overrides[step]
    if role in preset.roles:
        return preset.roles[role]
    return code_default


def resolve_search(*, routing: RoutingConfig | None = None, env_value: str | None = None,
                   configurable_value: str | None = None, code_default: str | None = None) -> str | None:
    """Resolve the search backend: env > active preset search > configurable > code default."""
    if env_value:
        return env_value
    routing = routing or load_routing()
    preset = routing.active()
    if preset.search:
        return preset.search
    if configurable_value is not None:
        return configurable_value
    return code_default


def apply_backend_env(routing: RoutingConfig | None = None) -> None:
    """Push active-preset backend settings into os.environ via setdefault (explicit env wins).

    Lets claude_agent_chat.py's existing getenv() calls read CLI bin/args/trust/sandbox from
    the routing file without any change to that module.
    """
    routing = routing or load_routing()
    g = routing.backends.get("gemini")
    if g:
        if g.cli_bin is not None:
            os.environ.setdefault("GEMINI_CLI_BIN", g.cli_bin)
        os.environ.setdefault("GEMINI_CLI_ARGS", " ".join(g.cli_args))
        os.environ.setdefault("GEMINI_SEARCH_ARGS", " ".join(g.cli_args))
        if g.trust_workspace is not None:
            os.environ.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true" if g.trust_workspace else "false")
    c = routing.backends.get("codex")
    if c:
        if c.cli_bin is not None:
            os.environ.setdefault("CODEX_CLI_BIN", c.cli_bin)
        if c.sandbox is not None:
            os.environ.setdefault("CODEX_SANDBOX", c.sandbox)
