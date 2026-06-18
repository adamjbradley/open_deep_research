"""Run-start preflight: probe the active preset's primary backends before work begins."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    """Raised under ODR_PREFLIGHT=fail when a primary backend is unusable."""


def primary_backends(preset) -> set[str]:
    from open_deep_research.failover import backend_of
    heads = set()
    for spec in preset.roles.values():
        chain = [spec] if isinstance(spec, str) else list(spec)
        if chain:
            heads.add(backend_of(chain[0]))
    return heads


def _probe_uncached(backend: str) -> bool:
    """True if the backend looks usable. Claude (subscription) always True; gemini/codex probed via CLI."""
    if backend in ("claude", "anthropic"):
        return True
    if backend in ("gemini", "google"):
        binname = os.environ.get("GEMINI_CLI_BIN", "gemini")
        if shutil.which(binname) is None:
            return False
        try:
            # Cheap, non-interactive: checks only that the binary is present and runnable.
            # A logged-out gemini CLI still exits 0 for --version, so this does NOT validate
            # authentication. Logged-out detection is handled reactively by backend-fatal
            # classification (G1/G4) on the first real call.
            r = subprocess.run([binname, "--version"], capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    if backend == "codex":
        return shutil.which(os.environ.get("CODEX_CLI_BIN", "codex")) is not None
    return True


_probe_cache: dict[str, bool] = {}


def probe_backend(backend: str) -> bool:
    if backend not in _probe_cache:
        _probe_cache[backend] = _probe_uncached(backend)
    return _probe_cache[backend]


def run_preflight(routing, tracker, *, policy: str | None = None) -> list[str]:
    policy = (policy or os.environ.get("ODR_PREFLIGHT", "warn")).strip().lower()
    if policy == "off":
        return []
    preset = routing.active()
    unusable = sorted(b for b in primary_backends(preset) if not probe_backend(b))
    if not unusable:
        return []
    msg = (f"preflight: active preset primary backend(s) {unusable} not usable "
           f"(e.g. gemini CLI not logged in -> run `gemini auth login`, or set MODEL_ROUTING_PRESET)")
    if policy == "fail":
        raise PreflightError(msg)
    logger.warning("%s; marking down so the run uses backups", msg)
    for b in unusable:
        tracker.mark_backend_down(b)
    return unusable
