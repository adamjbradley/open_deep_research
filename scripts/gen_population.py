"""Regenerate factbase/data/population.yaml from the World Bank API (author-time only).

Run: uv run --with pycountry python scripts/gen_population.py
Pulls SP.POP.TOTL most-recent-non-empty value per economy, keeps only real ISO-3166
alpha-3 countries (drops World Bank aggregates like WLD/EUU/AFE by intersecting with
pycountry), writes {ALPHA3: {value, year}}. Runtime never imports this; it reads the YAML.
"""
import datetime
import json
import os
import urllib.request

import pycountry
import yaml

URL = ("https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL"
       "?format=json&mrnev=1&per_page=400")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "open_deep_research",
                   "factbase", "data", "population.yaml")


def main() -> None:
    valid = {c.alpha_3 for c in pycountry.countries}  # real ISO-3166 countries only
    with urllib.request.urlopen(URL, timeout=60) as resp:  # noqa: S310 - fixed trusted URL
        payload = json.load(resp)
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    out = {}
    for r in rows:
        code = (r.get("countryiso3code") or "").strip()
        val = r.get("value")
        year = r.get("date")
        if code in valid and val is not None and year:
            out[code] = {"value": int(val), "year": int(year)}
    header = (f"# Generated {datetime.date.today().isoformat()} from World Bank "
              f"SP.POP.TOTL (most-recent value per country). Regenerate: scripts/gen_population.py\n")
    with open(os.path.normpath(OUT), "w", encoding="utf-8") as fh:
        fh.write(header)  # YAML comment -> invisible to safe_load, visible in git blame/vintage
        yaml.safe_dump(out, fh, sort_keys=True)
    print(f"wrote {len(out)} countries to {os.path.normpath(OUT)}")  # noqa: T201


if __name__ == "__main__":
    main()
