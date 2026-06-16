"""Regenerate factbase/data/iso3166.yaml from pycountry (author-time only).

Run: uv run --with pycountry python scripts/gen_iso3166.py
Runtime never imports this; it reads the committed YAML.
"""
import os

import pycountry
import yaml

OUT = os.path.join(os.path.dirname(__file__), "..", "src", "open_deep_research",
                   "factbase", "data", "iso3166.yaml")

# Hand-maintained common aliases/exonyms not in pycountry's primary name.
ALIASES = {
    "USA": ["United States", "US", "USA", "America"],
    "GBR": ["United Kingdom", "UK", "Britain", "Great Britain"],
    "KOR": ["South Korea", "Korea"],
    "PRK": ["North Korea"],
    "ARE": ["United Arab Emirates", "UAE"],
    "RUS": ["Russia"],
    "TUR": ["Turkey", "Turkiye", "Türkiye"],
    "CIV": ["Ivory Coast", "Cote d'Ivoire", "Côte d'Ivoire"],
    "CZE": ["Czech Republic", "Czechia"],
    "VEN": ["Venezuela"],
    "BOL": ["Bolivia"],
    "IRN": ["Iran"],
    "SYR": ["Syria"],
    "LAO": ["Laos"],
    "TZA": ["Tanzania"],
    "VNM": ["Vietnam"],
}


def main() -> None:
    """Generate the ISO-3166 YAML data file from pycountry."""
    out = {}
    for c in pycountry.countries:
        names = [c.name]
        for attr in ("official_name", "common_name"):
            v = getattr(c, attr, None)
            if v and v not in names:
                names.append(v)
        names.extend(a for a in ALIASES.get(c.alpha_3, []) if a not in names)
        out[c.alpha_3] = names
    with open(os.path.normpath(OUT), "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=True)
    print(f"wrote {len(out)} countries to {os.path.normpath(OUT)}")  # noqa: T201


if __name__ == "__main__":
    main()
