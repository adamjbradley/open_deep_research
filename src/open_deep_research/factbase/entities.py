from __future__ import annotations

import re
import unicodedata

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    # Fold diacritics (ü -> u, ô -> o) so aliases match regardless of accents.
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NORM.sub("", stripped.lower())


_ALPHA3: dict[str, str] = {
    "france": "FRA", "turkiye": "TUR", "turkey": "TUR", "cotedivoire": "CIV", "ivorycoast": "CIV",
    "india": "IND", "estonia": "EST", "singapore": "SGP", "nigeria": "NGA", "kenya": "KEN", "brazil": "BRA",
    "indonesia": "IDN", "pakistan": "PAK", "philippines": "PHL", "ukraine": "UKR", "rwanda": "RWA",
    "peru": "PER", "bangladesh": "BGD", "ethiopia": "ETH", "morocco": "MAR", "mexico": "MEX",
}


class CountryResolver:
    def resolve(self, name: str) -> str | None:
        return _ALPHA3.get(_norm(name))
