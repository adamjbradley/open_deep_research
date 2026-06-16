from open_deep_research.factbase.matrix import build_matrix, render_matrix


def _grouped(instance_key, property_name, value, admission="provisional"):
    # Shape mirrors query.group_by_canonical output rows.
    return {"instance_key": instance_key, "property_name": property_name,
            "value": value, "admission": admission, "in_conflict": False}


def test_build_matrix_rows_by_instance_cols_by_property():
    rows = [
        _grouped("NGA", "cbdc_launch_status", "launched", "trusted"),
        _grouped("NGA", "cbdc_ledger_architecture", "centralized"),
        _grouped("IND", "cbdc_launch_status", "pilot"),
    ]
    m = build_matrix(rows, ["cbdc_launch_status", "cbdc_ledger_architecture"],
                     label=lambda k: {"NGA": "Nigeria", "IND": "India"}[k])
    # ordered by instance label
    assert [r["instance"] for r in m] == ["India", "Nigeria"]
    nga = next(r for r in m if r["instance"] == "Nigeria")
    assert nga["cells"]["cbdc_launch_status"] == "launched*"     # * marks trusted
    assert nga["cells"]["cbdc_ledger_architecture"] == "centralized"
    ind = next(r for r in m if r["instance"] == "India")
    assert ind["cells"]["cbdc_ledger_architecture"] == ""        # coverage gap


def test_render_markdown_has_header_and_rows():
    rows = [_grouped("NGA", "cbdc_launch_status", "launched", "trusted")]
    out = render_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria", fmt="md")
    assert "| country | cbdc_launch_status |" in out
    assert "| Nigeria | launched* |" in out


def test_render_csv():
    rows = [_grouped("NGA", "cbdc_launch_status", "launched")]
    out = render_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria", fmt="csv")
    assert out.splitlines()[0] == "country,cbdc_launch_status"
    assert out.splitlines()[1] == "Nigeria,launched"


def test_multi_value_cell_joins_sorted():
    rows = [
        _grouped("NGA", "cbdc_launch_status", "launched"),
        _grouped("NGA", "cbdc_launch_status", "design"),
    ]
    m = build_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria")
    # two distinct values for one (instance, property) -> joined, sorted
    assert m[0]["cells"]["cbdc_launch_status"] == "design; launched"


def test_conflict_marker_and_cooccurrence():
    rows = [
        {"instance_key": "NGA", "property_name": "cbdc_launch_status",
         "value": "launched", "admission": "trusted", "in_conflict": True},
    ]
    out = build_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria")
    assert out[0]["cells"]["cbdc_launch_status"] == "launched*!"  # trusted + conflict


def test_render_text_aligns_columns():
    rows = [_grouped("NGA", "cbdc_launch_status", "launched", "trusted")]
    out = render_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria", fmt="text")
    lines = out.splitlines()
    assert lines[0].startswith("country")          # header present
    assert "cbdc_launch_status" in lines[0]
    assert "Nigeria" in lines[1] and "launched*" in lines[1]


def test_render_md_escapes_pipe():
    rows = [_grouped("NGA", "cbdc_name", "tier 1 | tier 2")]
    out = render_matrix(rows, ["cbdc_name"], lambda k: "Nigeria", fmt="md")
    assert r"tier 1 \| tier 2" in out   # pipe escaped, table not broken
