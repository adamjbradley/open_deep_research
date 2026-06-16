"""Pydantic meta-schema for the source registry; builds the entries dict."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class _SourceEntry(BaseModel):
    domain: str
    tier: Literal["unvetted", "reputable", "authoritative"]
    flags: list[str] = []


class RegistryModel(BaseModel):
    version: str = "1"
    sources: list[_SourceEntry]


def registry_from_dict(data: dict) -> dict[str, dict]:
    """Validate a parsed registry dict and return the ``{domain: {tier, flags}}`` map."""
    model = RegistryModel.model_validate(data)
    return {s.domain: {"tier": s.tier, "flags": list(s.flags)} for s in model.sources}
