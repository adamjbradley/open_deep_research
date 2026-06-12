from open_deep_research.factbase import render

ROWS = [
    {"instance_key":"IND","property_name":"id_coverage_pct","qualifiers":{"population_basis":"adults_15plus"},
     "as_of":2024,"value":"99","unit":"%","admission":"provisional","in_conflict":True,
     "source_url":"https://id4d.worldbank.org/x","source_tier":"authoritative"},
    {"instance_key":"IND","property_name":"id_coverage_pct","qualifiers":{"population_basis":"adults_15plus"},
     "as_of":2024,"value":"87","unit":"%","admission":"provisional","in_conflict":True,
     "source_url":"https://gsma.com/y","source_tier":"authoritative"},
]

def test_text_marks_conflict_and_provisional():
    out = render.render(ROWS, fmt="text")
    assert "⚠" in out          # conflict marker present
    assert "~prov" in out       # provisional marker present
    assert "99" in out and "87" in out and "id4d.worldbank.org" in out

def test_csv_has_header_and_rows():
    out = render.render(ROWS, fmt="csv")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[0].startswith("instance_key,property_name,")
    assert len(lines) == 3      # header + 2 rows

def test_md_is_a_table():
    out = render.render(ROWS, fmt="md")
    assert out.count("|") >= 6 and "---" in out

def test_empty_rows_message():
    assert "no facts" in render.render([], fmt="text").lower()
