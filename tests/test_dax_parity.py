import json
import shutil
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from dax_parity import cli, compare, config, fingerprint, queries, reference, tmdl

HERE = Path(__file__).parent
SHOP = HERE / "fixtures" / "shop"
EXAMPLE = HERE.parent / "examples" / "recovery-bi"

SHOP_TOML = """
[model]
pbip = "Shop.pbip"

[reference]
tables = { Sales = "data/sales.csv" }
from = "Sales"

[[measure]]
name = "Revenue"
sql = "SUM(Sales.Amount)"
compare = "money"

[[measure]]
name = "Big Orders"
sql = "COUNT(*) FILTER (WHERE Sales.Amount > 100)"
compare = "exact"
blank_if_zero = true

[[slice]]
id = "totals"

[[slice]]
id = "by_region"
by = ["'Sales'[Region]"]
"""


@pytest.fixture
def shop(tmp_path) -> config.Suite:
    root = tmp_path / "shop"
    shutil.copytree(SHOP, root)
    (root / "dax-parity.toml").write_text(SHOP_TOML, encoding="utf-8")
    return config.load(root / "dax-parity.toml")


def recorded(rows_by_slice: dict) -> dict:
    return {"queries": {k: {"rows": v} for k, v in rows_by_slice.items()}}


# --- reading the project --------------------------------------------------------------------

def test_measures_single_multiline_fenced_and_quoted():
    project = tmdl.locate(SHOP / "Shop.pbip")
    m = tmdl.measures(project.definition)
    assert m["Revenue"] == "SUM ( Sales[Amount] )"
    assert tmdl.normalize(m["Big Orders"]) == "CALCULATE ( COUNTROWS ( Sales ), Sales[Amount] > 100 )"
    assert m["Bob's Share"] == "DIVIDE ( [Revenue], 2 )"
    assert set(m) == {"Revenue", "Big Orders", "Bob's Share"}


def test_parameters_are_read_and_rewritten(tmp_path):
    shutil.copytree(SHOP, tmp_path / "s")
    definition = tmdl.locate(tmp_path / "s" / "Shop.pbip").definition
    assert tmdl.parameters(definition) == {"DataFolder": "C:\\old\\data"}
    tmdl.set_parameters(definition, {"DataFolder": 'D:\\new "data"'})
    assert tmdl.parameters(definition) == {"DataFolder": 'D:\\new "data"'}
    with pytest.raises(ValueError):
        tmdl.set_parameters(definition, {"Missing": "x"})


# --- queries --------------------------------------------------------------------------------

@pytest.mark.parametrize("ref, table, column", [
    ("'Sales'[Region]", "Sales", "Region"),
    ("Sales[Region]", "Sales", "Region"),
    ("'Bob''s Table'[Col]]x]", "Bob's Table", "Col]x"),
])
def test_column_references(ref, table, column):
    c = config.Column.parse(ref)
    assert (c.table, c.column) == (table, column)
    assert config.Column.parse(c.dax) == c


def test_queries_quote_names(shop):
    q = {x["id"]: x["dax"] for x in queries.build(shop)}
    assert q["totals"] == 'EVALUATE ROW ( "Revenue", [Revenue], "Big Orders", [Big Orders] )'
    assert q["by_region"].startswith("EVALUATE SUMMARIZECOLUMNS ( 'Sales'[Region], ")
    s = config.Slice("x", (), ('Say "hi"]',))
    assert queries.dax(s) == 'EVALUATE ROW ( "Say ""hi""]", [Say "hi"]]] )'


# --- reference ------------------------------------------------------------------------------

def test_reference_groups_and_blanks(shop):
    ref = reference.compute(shop)
    assert ref["totals"] == [{"Revenue": 230.0, "Big Orders": 1}]
    by = {r["Sales[Region]"]: r for r in ref["by_region"]}
    assert by["North"] == {"Sales[Region]": "North", "Revenue": 200.0, "Big Orders": 1}
    assert by["South"]["Big Orders"] is None  # COUNT 0 -> BLANK, like COUNTROWS over no rows
    assert by["East"] == {"Sales[Region]": "East", "Revenue": 0.0, "Big Orders": None}


def test_rows_where_every_measure_is_blank_are_dropped(shop):
    shop.measures["Revenue"] = config.Measure("Revenue", "NULLIF(SUM(Sales.Amount), 0)", 0.005, False)
    regions = {r["Sales[Region]"] for r in reference.compute(shop)["by_region"]}
    assert regions == {"North", "South"}


def test_bad_sql_is_a_config_error(shop):
    shop.measures["Revenue"] = config.Measure("Revenue", "SUM(Nope)", 0, False)
    with pytest.raises(config.ConfigError, match="reference SQL failed"):
        reference.compute(shop)


# --- comparison -----------------------------------------------------------------------------

def good_rows():
    return {
        "totals": [{"[Revenue]": 230.0, "[Big Orders]": 1}],
        "by_region": [
            {"Sales[Region]": "North", "[Revenue]": 200.0, "[Big Orders]": 1},
            {"Sales[Region]": "South", "[Revenue]": 30.0, "[Big Orders]": None},
            {"Sales[Region]": "East", "[Revenue]": 0.0, "[Big Orders]": None},
        ],
    }


def test_identical_results_pass(shop):
    r = compare.compare(shop, recorded(good_rows()), reference.compute(shop))
    assert r.ok and r.cells == 8


def test_each_kind_of_difference(shop):
    rows = good_rows()
    rows["totals"][0]["[Revenue]"] = 230.004                # within half a cent: fine
    rows["by_region"][0]["[Revenue]"] = 200.01              # a cent off: value
    rows["by_region"][2]["[Revenue]"] = None                # BLANK where 0 is expected
    del rows["by_region"][1]                                # South missing
    rows["by_region"].append({"Sales[Region]": "West", "[Revenue]": 5.0, "[Big Orders]": None})
    r = compare.compare(shop, recorded(rows), reference.compute(shop))
    kinds = {(m.key, m.measure): m.kind for m in r.mismatches}
    assert kinds == {
        (("North",), "Revenue"): "value",
        (("East",), "Revenue"): "blank_vs_zero",
        (("South",), "Revenue"): "missing_row",
        (("West",), "Revenue"): "extra_row",
    }
    text = compare.render(r)
    assert "hint (blank_vs_zero)" in text and "Revenue: 4 of" in text


def test_junit_has_one_case_per_measure_and_slice(shop):
    rows = good_rows()
    rows["totals"][0]["[Big Orders]"] = 2
    xml = ET.fromstring(compare.junit(compare.compare(shop, recorded(rows), reference.compute(shop))))
    assert xml.get("tests") == "4" and xml.get("failures") == "1"
    failed = [c for c in xml if c.find("failure") is not None]
    assert [(c.get("classname"), c.get("name")) for c in failed] == [("dax-parity.totals", "Big Orders")]


# --- staleness ------------------------------------------------------------------------------

def test_changed_measure_makes_results_stale(shop):
    before = fingerprint.current(shop)
    assert fingerprint.stale(before, fingerprint.current(shop)) == []
    path = tmdl.locate(shop.pbip).definition / "tables" / "Sales.tmdl"
    path.write_text(path.read_text().replace("SUM ( Sales[Amount] )", "SUM ( Sales[Amount] ) + 0"))
    assert fingerprint.stale(before, fingerprint.current(shop)) == [
        "measure 'Revenue' changed since the results were recorded"]


def test_whitespace_only_edits_are_not_stale(shop):
    before = fingerprint.current(shop)
    path = tmdl.locate(shop.pbip).definition / "tables" / "Sales.tmdl"
    path.write_text(path.read_text().replace("SUM ( Sales[Amount] )", "SUM (   Sales[Amount] )"))
    assert fingerprint.stale(before, fingerprint.current(shop)) == []


def test_check_refuses_stale_results(shop, capsys):
    data = {"fingerprint": fingerprint.current(shop), **recorded(good_rows())}
    shop.results.parent.mkdir(parents=True)
    shop.results.write_text(json.dumps(data))
    toml = str(shop.root / "dax-parity.toml")
    assert cli.main(["-c", toml, "check"]) == cli.OK
    (shop.root / "data" / "sales.csv").write_text("Region,Amount\nNorth,1\n")
    assert cli.main(["-c", toml, "check"]) == cli.STALE
    assert "data file 'sales.csv' changed" in capsys.readouterr().out


# --- the CLI end to end ---------------------------------------------------------------------

def test_init_lists_every_measure(tmp_path):
    shutil.copytree(SHOP, tmp_path / "s")
    target = tmp_path / "s" / "dax-parity.toml"
    assert cli.main(["-c", str(target), "init", str(tmp_path / "s" / "Shop.pbip")]) == cli.OK
    raw = tomllib.loads(target.read_text())
    assert [m["name"] for m in raw["measure"]] == ["Revenue", "Big Orders", "Bob's Share"]
    assert "DataFolder" in raw["model"]["parameters"]


def test_recorded_example_matches_its_reference(tmp_path):
    """The Recovery BI example, recorded in Power BI Desktop: 17,885 cells, none different."""
    junit = tmp_path / "out.xml"
    assert cli.main(["-c", str(EXAMPLE / "dax-parity.toml"), "check", "--junit", str(junit)]) == cli.OK
    assert ET.parse(junit).getroot().get("failures") == "0"
