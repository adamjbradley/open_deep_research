# Model Routing as Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every model/search choice through a single validated, dynamically-read `model_routing.json` (presets + per-role + per-step overrides + per-backend settings), resolved beneath the existing config so the graph and backend code stay unchanged.

**Architecture:** A new `model_routing.py` provides a Pydantic meta-schema, a loader, a layered resolver (`env > step_override > role > preset > code-default`), and an `apply_backend_env` that populates `os.environ.setdefault(...)` from the active backends block. `Configuration.from_runnable_config` consults the resolver for the `*_model`/`search_api` fields; a new `Configuration.model_for(step, role)` handles per-step overrides at the extraction seam. A bundled default file reproduces today's committed gemini behavior.

**Tech Stack:** Python 3.11, Pydantic v2, stdlib `json`/`importlib.resources`, `pytest` with `asyncio.run()` (codebase convention — NOT pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-06-17-model-routing-as-data-design.md`.

**Branch:** continue on `spec/model-routing-as-data`.

---

## File Structure
- Create `src/open_deep_research/data/__init__.py` — make `open_deep_research.data` a package.
- Create `src/open_deep_research/data/model_routing.json` — bundled default (active preset = gemini).
- Create `src/open_deep_research/model_routing.py` — schema + loader + resolver + apply_backend_env.
- Modify `src/open_deep_research/configuration.py` — `from_runnable_config` consults the resolver; add `model_for`.
- Modify `src/open_deep_research/deep_researcher.py` — extraction seam uses `model_for("extract_facts", "researcher")`.
- Modify `src/open_deep_research/factbase/dossier.py` — `validate` also validates `model_routing.json`.
- Modify `pyproject.toml` — package `open_deep_research.data` + `*.json`.
- Modify `.env.example` — document `MODEL_ROUTING_FILE`/`MODEL_ROUTING_PRESET`.
- Tests: `test_model_routing_schema.py`, `test_model_routing_resolve.py`, `test_model_routing_backend_env.py`, `test_model_routing_config_integration.py`, `test_model_routing_validate_cli.py`.

**Known roles** (the 6 `*_model` fields, minus the `_model` suffix): `supervisor`, `researcher`, `summarization`, `compression`, `final_report`, `facts_answer_polish`.
**Known per-step override keys (allowlist):** `extract_facts` (only site that adopts `model_for` in this plan).
**Known backends:** `claude`, `gemini`, `codex`. **Known model prefixes:** `claude`, `gemini`, `google`, `codex`, `openai`, `anthropic`, or bare (no prefix).

---

### Task 1: Bundled default file + packaging

**Files:**
- Create: `src/open_deep_research/data/__init__.py`
- Create: `src/open_deep_research/data/model_routing.json`
- Modify: `pyproject.toml`
- Test: `tests/test_model_routing_packaging.py`

- [ ] **Step 1: Create the package marker**

`src/open_deep_research/data/__init__.py`:
```python
"""Bundled top-level data (model_routing.json) read via importlib.resources."""
```

- [ ] **Step 2: Create the bundled default routing file**

`src/open_deep_research/data/model_routing.json`:
```json
{
  "version": "1",
  "active_preset": "gemini",
  "backends": {
    "gemini": { "cli_bin": "gemini", "cli_args": [], "trust_workspace": true,
                "model_aliases": { "flash": "gemini-2.5-flash" } },
    "codex":  { "cli_bin": "codex", "sandbox": "read-only" },
    "claude": { "subscription": true }
  },
  "presets": {
    "gemini": {
      "roles": {
        "supervisor": "gemini:gemini-2.5-flash",
        "researcher": "gemini:gemini-2.5-flash",
        "summarization": "gemini:gemini-2.5-flash",
        "compression": "gemini:gemini-2.5-flash",
        "final_report": "gemini:gemini-2.5-flash",
        "facts_answer_polish": "gemini:gemini-2.5-flash"
      },
      "search": "tavily",
      "step_overrides": {}
    },
    "claude": {
      "roles": {
        "supervisor": "claude:sonnet",
        "researcher": "claude:sonnet",
        "summarization": "claude:sonnet",
        "compression": "claude:sonnet",
        "final_report": "claude:sonnet",
        "facts_answer_polish": "claude:sonnet"
      },
      "search": "tavily",
      "step_overrides": {}
    }
  }
}
```

- [ ] **Step 3: Package the data dir**

In `pyproject.toml`, add `"open_deep_research.data"` to the `[tool.setuptools] packages` list (next to `"open_deep_research.factbase.data"`), and add a package-data entry under `[tool.setuptools.package-data]`:
```toml
"open_deep_research.data" = ["*.json"]
```
(Mirror exactly how `open_deep_research.factbase.data` is declared in both places.)

- [ ] **Step 4: Write the packaging test**

`tests/test_model_routing_packaging.py`:
```python
import json
from importlib.resources import files


def test_bundled_routing_is_importable_and_valid_json():
    text = files("open_deep_research.data").joinpath("model_routing.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["active_preset"] == "gemini"
    assert "gemini" in data["presets"] and "claude" in data["presets"]
    assert data["presets"]["gemini"]["roles"]["researcher"] == "gemini:gemini-2.5-flash"
```

- [ ] **Step 5: Run it**

Run: `uv run pytest tests/test_model_routing_packaging.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/data/__init__.py src/open_deep_research/data/model_routing.json \
        pyproject.toml tests/test_model_routing_packaging.py
git commit -m "feat(routing): bundle default model_routing.json + package data dir"
```

---

### Task 2: Meta-schema + loader

**Files:**
- Create: `src/open_deep_research/model_routing.py`
- Test: `tests/test_model_routing_schema.py`

- [ ] **Step 1: Write the failing test**

`tests/test_model_routing_schema.py`:
```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_model_routing_schema.py -q`
Expected: FAIL — module `model_routing` missing.

- [ ] **Step 3: Implement the schema + loader**

`src/open_deep_research/model_routing.py`:
```python
"""Model/search routing as data: a validated, dynamically-read model_routing.json that
holds named presets, per-role models, per-step overrides, and per-backend settings.

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
    cli_bin: Optional[str] = None
    cli_args: list[str] = []
    trust_workspace: Optional[bool] = None
    sandbox: Optional[str] = None
    subscription: Optional[bool] = None
    model_aliases: dict[str, str] = {}


class Preset(BaseModel):
    roles: dict[str, str] = {}
    search: Optional[str] = None
    step_overrides: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> "Preset":
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
    version: str = "1"
    active_preset: str
    backends: dict[str, BackendSettings] = {}
    presets: dict[str, Preset]

    @model_validator(mode="after")
    def _check(self) -> "RoutingConfig":
        if self.active_preset not in self.presets:
            raise ValueError(f"active_preset {self.active_preset!r} not in presets {sorted(self.presets)}")
        for name in self.backends:
            if name not in KNOWN_BACKENDS:
                raise ValueError(f"unknown backend {name!r} (known: {sorted(KNOWN_BACKENDS)})")
        return self

    def active(self) -> "Preset":
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_routing_schema.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/model_routing.py tests/test_model_routing_schema.py
git commit -m "feat(routing): model_routing meta-schema + validated loader"
```

---

### Task 3: Resolver (`resolve_model` / `resolve_search`)

**Files:**
- Modify: `src/open_deep_research/model_routing.py`
- Test: `tests/test_model_routing_resolve.py`

- [ ] **Step 1: Write the failing test**

`tests/test_model_routing_resolve.py`:
```python
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


def test_configurable_beats_code_default():
    assert resolve_model("compression", routing=_R, env_value=None,
                         configurable_value="claude:opus", code_default="claude:haiku") == "claude:opus"


def test_preset_switch_via_env(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "claude")
    assert resolve_model("researcher", routing=_R, env_value=None,
                         configurable_value=None, code_default="x") == "claude:sonnet"


def test_resolve_search_role_then_env(monkeypatch):
    assert resolve_search(routing=_R, env_value=None, configurable_value=None, code_default="none") == "tavily"
    assert resolve_search(routing=_R, env_value="codex", configurable_value=None, code_default="none") == "codex"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_model_routing_resolve.py -q`
Expected: FAIL — `resolve_model` not defined.

- [ ] **Step 3: Implement the resolver**

Append to `src/open_deep_research/model_routing.py`:
```python
def resolve_model(role: str, *, routing: "RoutingConfig | None" = None, step: str | None = None,
                  env_value: str | None = None, configurable_value: str | None = None,
                  code_default: str | None = None) -> str | None:
    """Resolve a model string by precedence: env > step_override > role > configurable > code default."""
    if env_value:
        return env_value
    routing = routing or load_routing()
    preset = routing.active()
    if step and step in preset.step_overrides:
        return preset.step_overrides[step]
    if role in preset.roles:
        return preset.roles[role]
    if configurable_value is not None:
        return configurable_value
    return code_default


def resolve_search(*, routing: "RoutingConfig | None" = None, env_value: str | None = None,
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_routing_resolve.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/model_routing.py tests/test_model_routing_resolve.py
git commit -m "feat(routing): layered resolve_model/resolve_search (env>step>role>default)"
```

---

### Task 4: `apply_backend_env`

**Files:**
- Modify: `src/open_deep_research/model_routing.py`
- Test: `tests/test_model_routing_backend_env.py`

- [ ] **Step 1: Write the failing test**

`tests/test_model_routing_backend_env.py`:
```python
from open_deep_research.model_routing import apply_backend_env, routing_from_dict

_R = routing_from_dict({
    "version": "1", "active_preset": "gemini",
    "backends": {"gemini": {"cli_bin": "gemini", "cli_args": [], "trust_workspace": True},
                 "codex": {"cli_bin": "codex", "sandbox": "read-only"}},
    "presets": {"gemini": {"roles": {"researcher": "gemini:gemini-2.5-flash"}}},
})


def test_apply_populates_gemini_env(monkeypatch):
    for k in ("GEMINI_CLI_BIN", "GEMINI_CLI_TRUST_WORKSPACE", "GEMINI_CLI_ARGS", "CODEX_SANDBOX"):
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_model_routing_backend_env.py -q`
Expected: FAIL — `apply_backend_env` not defined.

- [ ] **Step 3: Implement it**

Append to `src/open_deep_research/model_routing.py`:
```python
def apply_backend_env(routing: "RoutingConfig | None" = None) -> None:
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
```

NOTE for the implementer: `CODEX_SANDBOX` is a new env var this introduces; wiring it into the actual codex command (read-only sandbox flag) in `claude_agent_chat.py` is a deferred fast-follow (the spec keeps codex opt-in). This task only populates the env; the test asserts population, not codex behavior.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_routing_backend_env.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/model_routing.py tests/test_model_routing_backend_env.py
git commit -m "feat(routing): apply_backend_env populates CLI settings via setdefault"
```

---

### Task 5: Wire the resolver into `Configuration`

**Files:**
- Modify: `src/open_deep_research/configuration.py` (`from_runnable_config`, add `model_for`)
- Test: `tests/test_model_routing_config_integration.py`

- [ ] **Step 1: Write the failing test**

`tests/test_model_routing_config_integration.py`:
```python
from open_deep_research.configuration import Configuration


def test_config_uses_routing_preset_for_roles(monkeypatch, tmp_path):
    # no env model overrides -> roles come from the bundled gemini preset
    for k in ("RESEARCHER_MODEL", "SUPERVISOR_MODEL", "MODEL_ROUTING_FILE", "MODEL_ROUTING_PRESET"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "gemini:gemini-2.5-flash"
    assert c.supervisor_model == "gemini:gemini-2.5-flash"


def test_env_overrides_routing(monkeypatch):
    monkeypatch.setenv("RESEARCHER_MODEL", "claude:sonnet")
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "claude:sonnet"          # env wins
    assert c.supervisor_model == "gemini:gemini-2.5-flash"  # others unchanged


def test_preset_switch(monkeypatch):
    monkeypatch.delenv("RESEARCHER_MODEL", raising=False)
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "claude")
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "claude:sonnet"


def test_model_for_step_override(monkeypatch, tmp_path):
    # a routing file with a step override for extract_facts
    import json
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "g",
        "presets": {"g": {"roles": {"researcher": "gemini:gemini-2.5-flash"},
                          "step_overrides": {"extract_facts": "claude:sonnet"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    for k in ("RESEARCHER_MODEL",):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.researcher_model == "gemini:gemini-2.5-flash"
    assert c.model_for("extract_facts", "researcher") == "claude:sonnet"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_model_routing_config_integration.py -q`
Expected: FAIL — config returns the code default (gemini) but `model_for` missing; or roles not routed.

- [ ] **Step 3: Modify `from_runnable_config`**

In `src/open_deep_research/configuration.py`, replace the body of `from_runnable_config` with:
```python
    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig (+ model_routing.json)."""
        from open_deep_research.model_routing import (
            apply_backend_env, load_routing, resolve_model, resolve_search,
        )

        configurable = config.get("configurable", {}) if config else {}
        field_names = list(cls.model_fields.keys())
        routing = load_routing()
        apply_backend_env(routing)

        role_fields = {f"{r}_model" for r in (
            "supervisor", "researcher", "summarization", "compression",
            "final_report", "facts_answer_polish")}

        values: dict[str, Any] = {}
        for field_name in field_names:
            env_v = os.environ.get(field_name.upper())
            cfg_v = configurable.get(field_name)
            default = cls.model_fields[field_name].default
            if field_name in role_fields:
                role = field_name[: -len("_model")]
                values[field_name] = resolve_model(
                    role, routing=routing, env_value=env_v, configurable_value=cfg_v,
                    code_default=default)
            elif field_name == "search_api":
                code_default = default.value if hasattr(default, "value") else default
                values[field_name] = resolve_search(
                    routing=routing, env_value=env_v, configurable_value=cfg_v,
                    code_default=code_default)
            else:
                values[field_name] = env_v if env_v is not None else cfg_v
        return cls(**{k: v for k, v in values.items() if v is not None})

    def model_for(self, step: str, fallback_role: str) -> str:
        """Model for a specific graph step: env(role) > preset step_override > resolved role model."""
        from open_deep_research.model_routing import load_routing
        env_v = os.environ.get(f"{fallback_role}_model".upper())
        if env_v:
            return env_v
        preset = load_routing().active()
        if step in preset.step_overrides:
            return preset.step_overrides[step]
        return getattr(self, f"{fallback_role}_model")
```

(Keep the rest of the class unchanged. `Any`/`os`/`Optional`/`RunnableConfig` are already imported in this file.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_routing_config_integration.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Full suite (no regressions — defaults now flow through routing)**

Run: `uv run pytest tests/ -p no:warnings`
Expected: green. The bundled gemini preset matches the current `configuration.py` defaults, so behavior is unchanged; `test_codex_suited_roles.py` passes explicit configs and is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/configuration.py tests/test_model_routing_config_integration.py
git commit -m "feat(config): from_runnable_config resolves models via model_routing; add model_for"
```

---

### Task 6: Adopt `model_for` at the extraction seam

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`_make_fact_model_call`)
- Test: covered by `test_model_routing_config_integration.py::test_model_for_step_override` + a targeted assertion

- [ ] **Step 1: Find the model-selection line in `_make_fact_model_call`**

Read `_make_fact_model_call` in `deep_researcher.py`. It builds a `model_config` dict and calls `configurable_model.with_structured_output(...).with_config(model_config)`. Locate where `model_config["model"]` is set (it currently uses the configurable_model default or `configurable.researcher_model`).

- [ ] **Step 2: Write the failing test**

Append to `tests/test_model_routing_config_integration.py`:
```python
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
    c = Configuration.from_runnable_config({})
    # the extraction seam must resolve to the step override, not the researcher role
    assert c.model_for("extract_facts", "researcher") == "claude:sonnet"
```
(This asserts the contract `_make_fact_model_call` will use; the wiring change below makes the graph honor it.)

- [ ] **Step 3: Wire `model_for` into `_make_fact_model_call`**

In `_make_fact_model_call`, change the model assignment so the extraction model comes from `configurable.model_for("extract_facts", "researcher")` instead of the raw researcher model. Concretely, where `model_config` sets the model key, use:
```python
        "model": configurable.model_for("extract_facts", "researcher"),
```
Leave `max_tokens`/`api_key` as they are (they key off `researcher_model` / the resolved model). If the function currently relies on the `configurable_model` singleton default rather than an explicit model key, add the explicit `"model"` key to `model_config` so the per-step override takes effect.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_model_routing_config_integration.py -q`
Expected: PASS.
Run: `uv run pytest tests/ -p no:warnings`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_model_routing_config_integration.py
git commit -m "feat(graph): fact extraction honors per-step model_for('extract_facts')"
```

---

### Task 7: `dossier validate` + `.env.example` docs

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py` (`validate_profiles` or the `validate` handler)
- Modify: `.env.example`
- Test: `tests/test_model_routing_validate_cli.py`

- [ ] **Step 1: Write the failing test**

`tests/test_model_routing_validate_cli.py`:
```python
import asyncio
import json

from open_deep_research.factbase.dossier import run


def test_validate_accepts_good_routing(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "g",
        "presets": {"g": {"roles": {"researcher": "gemini:gemini-2.5-flash"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    out = asyncio.run(run(["validate"]))
    assert "model_routing.json" in out and "INVALID" not in out


def test_validate_rejects_bad_routing(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "missing",
        "presets": {"g": {"roles": {"researcher": "gemini:x"}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    out = asyncio.run(run(["validate"]))
    assert "INVALID" in out and "model_routing" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_model_routing_validate_cli.py -q`
Expected: FAIL — validate doesn't check the routing file yet.

- [ ] **Step 3: Add routing validation to the `validate` command**

In `dossier.py`, find `validate_profiles()` (returns `(report, ok)`). After the profile/registry loop, before returning, append a routing check:
```python
    # Validate the model routing file (env file > ./model_routing.json > bundled).
    try:
        from open_deep_research.model_routing import _routing_path, load_routing
        load_routing()  # raises on invalid
        src = _routing_path() or "model_routing.json (bundled)"
        lines.append(f"OK    {src}")
    except Exception as e:  # noqa: BLE001 - report-and-continue
        ok = False
        lines.append(f"FAIL  model_routing.json: {e}")
```
(The `load_routing()` memoization is keyed by (path, mtime); the test sets `MODEL_ROUTING_FILE` to a fresh tmp file so each case re-validates.)

IMPORTANT: `load_routing()` is `lru_cache`d. Across the two tests the path differs (different tmp files), so cache keys differ — fine. But if a single process validates the same path twice after editing, the mtime key handles it. No action needed.

- [ ] **Step 4: Document in `.env.example`**

Add under the backend section in `.env.example`:
```bash
# --- Model routing (model_routing.json) ---
# Per-role/per-step model + per-backend settings live in model_routing.json (presets:
# 'gemini' (default), 'claude'). Resolution: explicit env (SUPERVISOR_MODEL etc.) >
# file step_override > file preset role > code default. So these env vars still work as
# one-off overrides on top of the file.
# MODEL_ROUTING_FILE=./model_routing.json   # default: ./model_routing.json then bundled
# MODEL_ROUTING_PRESET=gemini               # flip the whole pipeline: gemini|claude
```

- [ ] **Step 5: Run the tests + full suite + validate**

Run: `uv run pytest tests/test_model_routing_validate_cli.py -q`
Expected: PASS.
Run: `uv run pytest tests/ -p no:warnings`
Expected: green.
Run: `uv run dossier validate`
Expected: output includes `OK ... model_routing.json (bundled)` (no `INVALID`).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py .env.example tests/test_model_routing_validate_cli.py
git commit -m "feat(dossier): validate model_routing.json; document routing env in .env.example"
```

---

## Self-Review

**Spec coverage:**
- Bundled default file + packaging → Task 1. ✓ (US-5 absent-file falls back: bundled IS the fallback; deleting the cwd file uses bundled.)
- Meta-schema + loader (validation, dynamic per-run via mtime) → Task 2. ✓ (US-6, US-7)
- Layered resolver env>step>role>preset>default → Task 3. ✓ (US-1 presets, US-2 per-step, US-4 env-wins)
- `apply_backend_env` (backend settings from data) → Task 4. ✓ (US-3)
- `from_runnable_config` integration + `model_for` → Task 5. ✓ (US-1, US-4, US-7)
- Extraction seam adopts `model_for` → Task 6. ✓ (US-2)
- `dossier validate` + `.env.example` → Task 7. ✓ (US-6)

**Placeholder scan:** No TBD/TODO. Task 6 Step 1/3 reference reading `_make_fact_model_call` to locate the exact model line — that's a concrete locate-then-edit instruction (the file's structure varies), with the exact replacement shown. The `CODEX_SANDBOX` codex-command wiring is explicitly marked a deferred fast-follow (out of scope per spec), not a placeholder in this plan.

**Type consistency:** `resolve_model(role, *, routing, step, env_value, configurable_value, code_default)` and `resolve_search(*, routing, env_value, configurable_value, code_default)` signatures identical across Tasks 3 and 5. `RoutingConfig.active() -> Preset`, `Preset.step_overrides`/`roles`/`search` used consistently. `load_routing()`/`routing_from_dict()`/`apply_backend_env()` names consistent across all tasks. `Configuration.model_for(step, fallback_role)` consistent in Tasks 5 and 6. `KNOWN_ROLES`/`KNOWN_STEPS`/`KNOWN_BACKENDS`/`KNOWN_PREFIXES`/`KNOWN_SEARCH` defined once in Task 2, referenced by the schema.

**Note for executor:** the bundled `model_routing.json` (Task 1) must keep parity with the committed `configuration.py` model defaults (currently `gemini:gemini-2.5-flash` for all roles) so Task 5's full-suite run stays green with no behavior change.
