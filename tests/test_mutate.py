import shutil
from pathlib import Path

import pytest

from dax_parity import config, mutate, mutations, reference

HERE = Path(__file__).parent
SHOP = HERE / "fixtures" / "shop"


def ops(dax: str, name: str = "M") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in mutations.mutants(name, dax):
        out.setdefault(m.operator, []).append(" ".join(m.expression.split()))
    return out


# --- operators ------------------------------------------------------------------------------

def test_aggregation_and_divide():
    got = ops("DIVIDE ( SUM ( Sales[Amount] ), 100 )")
    assert got["aggregation"] == ["DIVIDE ( AVERAGE ( Sales[Amount] ), 100 )"]
    assert got["divide"] == ["DIVIDE ( 100, SUM ( Sales[Amount] ) )"]
    assert got["constant"] == ["DIVIDE ( SUM ( Sales[Amount] ), 101 )"]


def test_drop_each_calculate_filter():
    got = ops('CALCULATE ( [Revenue], Sales[Region] = "North", ALL ( Product ) )')
    assert got["drop_filter"] == ['CALCULATE ( [Revenue], ALL ( Product ) )',
                                  'CALCULATE ( [Revenue], Sales[Region] = "North" )']
    assert got["comparison"] == ['CALCULATE ( [Revenue], Sales[Region] <> "North", ALL ( Product ) )']
    assert got["string"] == ['CALCULATE ( [Revenue], Sales[Region] = "__mutant__", ALL ( Product ) )']


def test_blank_handling():
    got = ops("DIVIDE ( [Claims] + 0, [Total] )")
    assert got["blank"] == ["DIVIDE ( [Claims], [Total] )"]
    assert "arithmetic" not in got  # "- 0" would be the same measure, so it isn't generated
    assert ops("COALESCE ( [Claims], 0 )")["blank"] == ["[Claims]"]


def test_var_assignment_is_not_a_comparison():
    got = ops("VAR x = SUM ( T[a] ) RETURN IF ( x > 0, x )")
    assert got["comparison"] == ["VAR x = SUM ( T[a] ) RETURN IF ( x >= 0, x )"]
    assert got["negate_if"] == ["VAR x = SUM ( T[a] ) RETURN IF ( NOT ( x > 0 ), x )"]


def test_text_inside_names_strings_and_comments_is_left_alone():
    dax = "// SUM here is a comment\nCALCULATE ( [x], 'SUM table'[a = b] = \"SUM\" )"
    got = ops(dax)
    assert "aggregation" not in got
    assert len(got["comparison"]) == 1


def test_unary_minus_is_not_swapped():
    assert "arithmetic" not in ops("-[Cost]")
    assert ops("[A] - [B]")["arithmetic"] == ["[A] + [B]"]


def test_booleans_and_logic():
    got = ops("CALCULATE ( [x], T[a] = TRUE () && T[b] = 1 )")
    assert got["boolean"] == ["CALCULATE ( [x], T[a] = FALSE () && T[b] = 1 )"]
    assert got["logical"] == ["CALCULATE ( [x], T[a] = TRUE () || T[b] = 1 )"]


def test_restrict_operators_and_ids_are_unique():
    ms = mutations.mutants("M", "SUM ( T[a] ) + SUM ( T[b] )", {"aggregation"})
    assert {m.operator for m in ms} == {"aggregation"}
    assert len({m.id for m in ms}) == len(ms) == 2


# --- which measures a mutant can reach ------------------------------------------------------

def test_references_skip_columns():
    assert mutations.references("CALCULATE ( [Sales], 'Date'[Year] = 2026, T[x] > [Target] )") == {"Sales", "Target"}


def test_dependents_are_transitive():
    deps = mutations.dependents({"A": "SUM ( T[x] )", "B": "[A] * 2", "C": "[B] + 1", "D": "1"})
    assert deps["A"] == {"A", "B", "C"}
    assert deps["D"] == {"D"}


# --- planning and scoring -------------------------------------------------------------------

@pytest.fixture
def shop(tmp_path) -> config.Suite:
    root = tmp_path / "shop"
    shutil.copytree(SHOP, root)
    (root / "dax-parity.toml").write_text("""
[model]
pbip = "Shop.pbip"
[reference]
tables = { Sales = "data/sales.csv" }
from = "Sales"
[[measure]]
name = "Revenue"
sql = "SUM(Sales.Amount)"
compare = "money"
[[slice]]
id = "by_region"
by = ["'Sales'[Region]"]
[[mutate.equivalent]]
id = "Revenue / constant / 2 -> 3"
reason = "example"
""", encoding="utf-8")
    return config.load(root / "dax-parity.toml")


def rows(revenue: dict) -> dict:
    return {"queries": {"by_region": {"rows": [
        {"Sales[Region]": k, "[Revenue]": v} for k, v in revenue.items()]}}}


GOOD = {"North": 200.0, "South": 30.0, "East": 0.0}


def test_plan_runs_only_what_a_mutant_can_reach(shop):
    jobs, untested = mutate.plan(shop)
    assert jobs[0]["id"] == mutate.BASELINE
    revenue = [j for j in jobs[1:] if j["measure"] == "Revenue"]
    assert [j["mutant"].operator for j in revenue] == ["aggregation"]
    assert all(q["id"] == "by_region" for j in revenue for q in j["queries"])
    # Big Orders and Bob's Share exist in the model but no slice compares them.
    assert {m.measure for m in untested} == {"Big Orders", "Bob's Share"}


def test_evaluate_classifies_every_outcome(shop):
    jobs, untested = mutate.plan(shop)
    expected = reference.compute(shop)
    agg = jobs[1]["id"]
    results = [{"id": mutate.BASELINE, **rows(GOOD)}, {"id": agg, **rows(GOOD | {"North": 100.0})}]
    report = mutate.evaluate(shop, jobs, results, expected, untested)
    status = {o.id: o.status for o in report.outcomes}
    assert status[agg] == "killed"
    assert {o.status for o in report.outcomes if o.measure == "Bob's Share"} == {"untested"}
    assert report.score == report.count("killed") / (report.count("killed") + report.count("untested"))
    text = mutate.render(report)
    assert "bugs the checks would miss" in text and "not compared on any slice" in text


def test_survivor_invalid_and_equivalent(shop):
    jobs, _ = mutate.plan(shop, only={"Revenue"})
    expected = reference.compute(shop)
    agg = jobs[1]["id"]
    survived = mutate.evaluate(shop, jobs, [{"id": mutate.BASELINE, **rows(GOOD)}, {"id": agg, **rows(GOOD)}], expected, [])
    assert [o.status for o in survived.outcomes] == ["survived"] and survived.score == 0
    invalid = mutate.evaluate(shop, jobs, [{"id": mutate.BASELINE, **rows(GOOD)}, {"id": agg, "error": "bad DAX"}],
                              expected, [])
    assert [o.status for o in invalid.outcomes] == ["invalid"] and invalid.score is None
    shop.equivalent = {agg: "both are the same here"}
    eq = mutate.evaluate(shop, jobs, [{"id": mutate.BASELINE, **rows(GOOD)}, {"id": agg, **rows(GOOD)}], expected, [])
    assert [o.status for o in eq.outcomes] == ["equivalent"] and eq.score is None
    assert "both are the same here" in mutate.render(eq)


def test_failing_baseline_stops_the_run(shop):
    jobs, _ = mutate.plan(shop)
    bad = [{"id": mutate.BASELINE, **rows(GOOD | {"South": 1.0})}]
    with pytest.raises(ValueError, match="already differs"):
        mutate.evaluate(shop, jobs, bad, reference.compute(shop), [])


def test_equivalent_entries_need_a_reason(tmp_path, shop):
    toml = shop.root / "dax-parity.toml"
    toml.write_text(toml.read_text().replace('reason = "example"', ""))
    with pytest.raises(config.ConfigError, match="reason"):
        config.load(toml)
