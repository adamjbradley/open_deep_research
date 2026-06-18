"""ISO-3166 country-name resolution and alpha-3 key reverse lookup."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    # Fold diacritics (ü -> u, ô -> o) so aliases match regardless of accents.
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NORM.sub("", stripped.lower())


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], dict[str, str]]:
    """Return (norm_name -> alpha3, alpha3 -> primary_name), loaded once from data."""
    import yaml
    from importlib.resources import files

    try:
        text = files("open_deep_research.factbase.data").joinpath("iso3166.yaml").read_text(
            encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "iso3166.yaml missing from open_deep_research.factbase.data — reinstall the package"
        ) from exc
    data = yaml.safe_load(text) or {}
    name_to_key: dict[str, str] = {}
    key_to_name: dict[str, str] = {}
    for alpha3, names in data.items():
        if not names:
            continue
        key_to_name[alpha3] = names[0]  # first entry is the primary display name
        for n in names:
            name_to_key.setdefault(_norm(n), alpha3)
    return name_to_key, key_to_name


class CountryResolver:
    def resolve(self, name: str) -> str | None:
        return _load()[0].get(_norm(name))

    def resolve_in_text(self, text: str) -> str | None:
        """Resolve a country mentioned anywhere in free text (e.g. a subject phrase).

        ``resolve`` is exact-match only, so a descriptive subject like
        "Estonia's digital identity scheme" yields None. This tries the whole string
        first, then scans contiguous word n-grams longest-first (so "South Korea" wins
        over a bare "Korea") and matches each against the exact resolver. Whole-token
        matching avoids the substring false positives of scanning the separator-stripped
        norm (e.g. "Romania" contains "oman"). Single tokens shorter than 4 chars are
        skipped so noise like "id"/"is"/"in" cannot match a 2-letter code alias.
        """
        if not text:
            return None
        key = self.resolve(text)
        if key:
            return key
        words = re.findall(r"[A-Za-z0-9]+", text)
        n = len(words)
        for size in range(min(n, 4), 0, -1):
            for i in range(n - size + 1):
                cand = " ".join(words[i:i + size])
                if size == 1 and len(_norm(cand)) < 4:
                    continue
                hit = self.resolve(cand)
                if hit:
                    return hit
        return None

    def instance_name(self, key: str) -> str:
        """Primary display name for an alpha-3 key (echoes the key if unknown)."""
        return _load()[1].get(key, key)
