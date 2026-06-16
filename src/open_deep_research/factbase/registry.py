from __future__ import annotations
from urllib.parse import urlparse
_TIER_RANK = {"unvetted": 0, "reputable": 1, "authoritative": 2}
class SourceRegistry:
    def __init__(self, entries: dict[str, dict]):
        self._entries = entries
    @classmethod
    def load(cls, name: str) -> "SourceRegistry":
        import yaml
        from importlib.resources import files
        from .registry_schema import registry_from_dict
        text = (
            files("open_deep_research.factbase.profiles")
            .joinpath(f"{name}.yaml")
            .read_text(encoding="utf-8")
        )
        return cls(registry_from_dict(yaml.safe_load(text)))
    def _match(self, url: str) -> dict | None:
        host = (urlparse(url).hostname or "").lower()
        for domain, entry in self._entries.items():
            if host == domain or host.endswith("." + domain):
                return entry
        return None
    def tier(self, url: str) -> str:
        m = self._match(url); return m["tier"] if m else "unvetted"
    def flags(self, url: str) -> list[str]:
        m = self._match(url); return list(m.get("flags", [])) if m else []
    def meets_bar(self, url: str, threshold: str) -> bool:
        return _TIER_RANK[self.tier(url)] >= _TIER_RANK[threshold]
