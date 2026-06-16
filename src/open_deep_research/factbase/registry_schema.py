"""Pydantic meta-schema for the source registry; builds the entries dict + version/hash."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel


class _SourceEntry(BaseModel):
    domain: str
    tier: Literal["unvetted", "reputable", "authoritative"]
    flags: list[str] = []


class RegistryModel(BaseModel):
    version: str = "1"
    sources: list[_SourceEntry]


def load_registry(data: dict) -> tuple[dict[str, dict], str, str]:
    """Validate a parsed registry dict; return (entries, version, registry_hash).

    ``registry_hash`` is a sha256 over the validated *semantic* model (sorted) -- the
    registry's content identity, parallel to a profile's hash.
    """
    model = RegistryModel.model_validate(data)
    entries = {s.domain: {"tier": s.tier, "flags": list(s.flags)} for s in model.sources}
    semantic = {"sources": sorted(
        ({"domain": s.domain, "tier": s.tier, "flags": sorted(s.flags)} for s in model.sources),
        key=lambda d: d["domain"])}
    digest = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return entries, model.version, digest


def registry_from_dict(data: dict) -> dict[str, dict]:
    """Validate a parsed registry dict and return the ``{domain: {tier, flags}}`` map."""
    return load_registry(data)[0]
