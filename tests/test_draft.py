"""The drafting loop with a stand-in for the Claude client: no network, no API key."""
import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from dax_parity import cli, config, draft, tmdl

HERE = Path(__file__).parent
SHOP = HERE / "fixtures" / "shop"

TOML = """
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
"""


@pytest.fixture
def shop(tmp_path) -> config.Suite:
    root = tmp_path / "shop"
    shutil.copytree(SHOP, root)
    (root / "dax-parity.toml").write_text(TOML, encoding="utf-8")
    return config.load(root / "dax-parity.toml")


def reply(**out) -> SimpleNamespace:
    body = {"sql": "", "compare": "ratio", "blank_if_zero": False, "assumptions": [], "unclear": ""} | out
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(body))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
    )


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def info(shop, name):
    return tmdl.measure_info(tmdl.locate(shop.pbip).definition)[name]


def test_the_model_never_sees_the_dax(shop):
    i = info(shop, "Big Orders")
    client = FakeClient(reply(sql="COUNT(*) FILTER (WHERE Sales.Amount > 100)", compare="exact", blank_if_zero=True))
    data = draft.describe_data(shop, draft.connect(shop, duckdb))
    d = draft.draft_one(client, shop, data, i, "claude-opus-5-5", "medium")
    assert d.sql and not d.error and d.attempts == 1
    sent = json.dumps(client.calls[0]["messages"]) + json.dumps(client.calls[0]["system"])
    assert "COUNTROWS" not in sent and "CALCULATE" not in sent
    assert "Big Orders" in sent and "values: 'East', 'North', 'South'" in sent
    assert client.calls[0]["output_config"]["format"]["schema"] == draft.OUTPUT


def test_failing_sql_goes_back_to_the_model(shop):
    client = FakeClient(reply(sql="SUM(Sales.Amonut)"), reply(sql="SUM(Sales.Amount)", compare="money"))
    data = draft.describe_data(shop, draft.connect(shop, duckdb))
    d = draft.draft_one(client, shop, data, info(shop, "Revenue"), "claude-opus-5-5", "medium")
    assert d.attempts == 2 and d.sql == "SUM(Sales.Amount)" and not d.error
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "failed in DuckDB" in feedback and "Amonut" in feedback


def test_gives_up_after_three_tries(shop):
    client = FakeClient(*[reply(sql="SUM(nope)") for _ in range(3)])
    data = draft.describe_data(shop, draft.connect(shop, duckdb))
    d = draft.draft_one(client, shop, data, info(shop, "Revenue"), "claude-opus-5-5", "medium")
    assert d.attempts == 3 and d.error


def test_drafts_file_round_trips_and_overrides(shop, capsys):
    drafts = [
        draft.Draft("Revenue", sql="SUM(Sales.Amount) * 1", compare="money", assumptions=["amounts are in USD"]),
        draft.Draft("Big Orders", sql="COUNT(*) FILTER (WHERE Sales.Amount > 100)", compare="exact", blank_if_zero=True),
        draft.Draft("Bob's Share", unclear="needs the revenue of Bob's region only"),
    ]
    path = shop.root / "drafts.toml"
    draft.write(drafts, "claude-opus-5-5", path)
    text = path.read_text()
    assert "# assumption: amounts are in USD" in text and "# unclear: needs the revenue" in text
    parsed = tomllib.loads(text)["measure"]
    assert [m["name"] for m in parsed] == ["Revenue", "Big Orders", "Bob's Share"] and "sql" not in parsed[2]
    suite = config.load(shop.root / "dax-parity.toml", path)
    assert suite.measures["Revenue"].sql == "SUM(Sales.Amount) * 1"
    assert suite.measures["Big Orders"].blank_if_zero
    assert "Bob's Share" not in suite.measures


def test_sql_with_quotes_is_written_safely(tmp_path):
    d = draft.Draft("M", sql="COUNT(*) FILTER (WHERE x = 'it''s')", compare="exact")
    parsed = tomllib.loads(draft.to_toml([d], "m"))["measure"][0]
    assert parsed["sql"] == d.sql


def test_cost_uses_list_prices():
    d = draft.Draft("M", input_tokens=1_000_000, output_tokens=100_000)
    assert draft.cost([d], "claude-opus-5-5") == pytest.approx(4.0 + 2.0)


def test_draft_without_sdk_explains_how_to_install(shop, monkeypatch, capsys):
    import builtins
    real = builtins.__import__

    def no_anthropic(name, *a, **k):
        if name == "anthropic":
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    code = cli.main(["-c", str(shop.root / "dax-parity.toml"), "draft", "--measure", "Revenue"])
    assert code == cli.ERROR and "dax-parity[ai]" in capsys.readouterr().err


def test_no_samples_sends_no_values(shop):
    with_values = draft.describe_data(shop, draft.connect(shop, duckdb))
    without = draft.describe_data(shop, draft.connect(shop, duckdb), samples=False)
    assert "'North'" in with_values and "from 0" in with_values
    assert "North" not in without and "from 0" not in without and "Amount: " in without
