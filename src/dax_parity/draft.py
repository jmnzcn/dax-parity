"""Drafting the reference with Claude, from what each measure is supposed to mean.

The model gets the measure's name, its /// description, format string and folder, plus the data:
tables, columns, a few values per text column and the FROM clause the reference runs over. It never
sees the DAX. That is the point: a reference written by reading the DAX would copy its mistakes, so a
disagreement between the two is only informative if they were written apart.

Every draft is run in DuckDB on every slice before it is accepted; if it fails, the error goes back
to the model to fix (up to two more tries). Drafts are written to their own TOML file for a person to
review, and `dax-parity check --drafts FILE` compares Power BI against them.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from dax_parity import tmdl
from dax_parity.config import Suite, _ident
from dax_parity.reference import _scan

MODEL = "claude-opus-5-5"
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}  # $/M tokens

SYSTEM = """\
You write the reference implementation of one Power BI measure as a DuckDB SQL aggregate expression.

The reference exists to catch bugs in the measure's DAX, so you are deliberately not shown the DAX.
Work only from the measure's name, description, format and the data. Implement what the description
says the number means, the way a careful analyst would, and do not guess at how the DAX might have
been written.

Rules for the SQL:
- One aggregate expression, as it would appear in SELECT. It is evaluated once per group over the
  FROM clause you are given, grouped by whatever columns a slice uses (or not grouped, for totals).
- DuckDB dialect. Quote identifiers that need it with double quotes; qualify columns with their
  table. Aggregate FILTER (WHERE ...) clauses, CASE, NULLIF and ::DOUBLE casts are all fine.
- Return NULL where the measure has no value. Return 0 where the description implies the answer is
  zero (for example a share of rows when none qualify but the group has rows).
- Use only tables and columns listed in the data description, and only the values shown for text
  columns when filtering on them.

Also choose:
- compare: "exact" for counts, "money" for currency amounts, "ratio" for everything else.
- blank_if_zero: true for counts of rows or distinct values. Power BI shows BLANK rather than 0 when
  nothing qualifies, and that convention should not be reported as a difference. False otherwise.

If the measure cannot be written as a single aggregate (for example it needs a running total or a
time-intelligence window), or the description is too vague to implement, leave sql empty and explain
in unclear. List every interpretation you had to choose in assumptions.
"""

OUTPUT = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "compare": {"type": "string", "enum": ["exact", "money", "ratio"]},
        "blank_if_zero": {"type": "boolean"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "unclear": {"type": "string"},
    },
    "required": ["sql", "compare", "blank_if_zero", "assumptions", "unclear"],
    "additionalProperties": False,
}


@dataclass
class Draft:
    measure: str
    sql: str = ""
    compare: str = "ratio"
    blank_if_zero: bool = False
    assumptions: list[str] = field(default_factory=list)
    unclear: str = ""
    error: str = ""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def describe_data(suite: Suite, con, samples: bool = True) -> str:
    """Tables, columns, types and a few values: what an analyst would look at before writing SQL.

    With samples=False only names, types and row counts are described: no value from the data is sent.
    """
    parts = []
    for table in suite.tables:
        t = _ident(table)
        rows = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        parts.append(f"table {table} ({rows:,} rows)")
        for name, kind, *_ in con.execute(f"DESCRIBE {t}").fetchall():
            c = _ident(name)
            line = f"  {name}: {kind}"
            if not samples:
                pass
            elif kind == "VARCHAR":
                n = con.execute(f"SELECT COUNT(DISTINCT {c}) FROM {t}").fetchone()[0]
                if n <= 25:
                    values = [v for (v,) in con.execute(f"SELECT DISTINCT {c} FROM {t} ORDER BY 1").fetchall()]
                    line += f", values: {', '.join(repr(v) for v in values)}"
                else:
                    q = f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL ORDER BY 1 LIMIT 3"
                    sample = [v for (v,) in con.execute(q).fetchall()]
                    line += f", {n:,} distinct, e.g. {', '.join(repr(v) for v in sample)}"
            elif kind != "BOOLEAN":
                lo, hi = con.execute(f"SELECT MIN({c}), MAX({c}) FROM {t}").fetchone()
                line += f", from {lo} to {hi}"
            parts.append(line)
    slices = [", ".join(c.sql for c in s.by) or "(grand total)" for s in suite.slices if not s.sql]
    parts += ["", "FROM clause the expression is evaluated over:", suite.from_sql.strip(), "",
              "It is grouped, one slice at a time, by: " + "; ".join(slices)]
    return "\n".join(parts)


def brief(info: tmdl.MeasureInfo) -> str:
    return "\n".join([
        f"Measure: {info.name}",
        f"Description: {info.description or '(none)'}",
        f"Format string: {info.format or '(none)'}",
        f"Display folder: {info.folder or '(none)'}",
    ])


def try_sql(suite: Suite, con, sql: str) -> str | None:
    """Run the expression on every generated slice; return the first error, or None."""
    for s in suite.slices:
        if s.sql:
            continue
        cols = ", ".join(c.sql for c in s.by)
        q = f"SELECT {cols + ', ' if cols else ''}({sql}) AS v FROM {suite.from_sql}"
        if cols:
            q += f" GROUP BY {cols}"
        try:
            con.execute(q).fetchall()
        except Exception as e:  # noqa: BLE001 - any DuckDB error goes back to the model
            return f"on slice {s.id}: {str(e).splitlines()[0]}"
    return None


def draft_one(client, suite: Suite, data: str, info: tmdl.MeasureInfo, model: str, effort: str) -> Draft:
    import duckdb

    con = connect(suite, duckdb)
    d = Draft(info.name)
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": "The data:\n\n" + data, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": brief(info)}]
    for attempt in range(1, 4):
        d.attempts = attempt
        response = client.messages.create(
            model=model, max_tokens=8000, system=system, messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT}},
        )
        d.input_tokens += response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0) \
            + (response.usage.cache_creation_input_tokens or 0)
        d.output_tokens += response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            d.error = f"the model stopped ({response.stop_reason})"
            return d
        out = json.loads(next(b.text for b in response.content if b.type == "text"))
        d.sql, d.compare, d.blank_if_zero = out["sql"].strip(), out["compare"], out["blank_if_zero"]
        d.assumptions, d.unclear = out["assumptions"], out["unclear"].strip()
        if not d.sql:
            return d
        error = try_sql(suite, con, d.sql)
        if error is None:
            d.error = ""
            return d
        d.error = error
        messages += [{"role": "assistant", "content": response.content},
                     {"role": "user", "content": f"That expression failed in DuckDB {error}. Fix it."}]
    return d


def connect(suite: Suite, duckdb):
    con = duckdb.connect()
    for name, path in suite.tables.items():
        con.execute(f"CREATE VIEW {_ident(name)} AS SELECT * FROM {_scan(path)}")
    return con


def run(suite: Suite, names: list[str], model: str = MODEL, effort: str = "medium", workers: int = 4,
        samples: bool = True) -> list[Draft]:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError('drafting needs the Anthropic SDK: pip install "dax-parity[ai]"') from e
    import duckdb

    if not suite.from_sql:
        raise ValueError("[reference] from = \"...\" is needed: the drafts are aggregates over it")
    info = tmdl.measure_info(tmdl.locate(suite.pbip).definition)
    missing = [n for n in names if n not in info]
    if missing:
        raise ValueError(f"not in the model: {missing}")
    data = describe_data(suite, connect(suite, duckdb), samples)
    client = anthropic.Anthropic()

    def one(name: str) -> Draft:
        d = draft_one(client, suite, data, info[name], model, effort)
        status = "error: " + d.error if d.error else ("unclear" if not d.sql else "ok")
        print(f"  {name}: {status}", file=sys.stderr)
        return d

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, names))


def cost(drafts: list[Draft], model: str) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return sum(d.input_tokens for d in drafts) / 1e6 * p_in + sum(d.output_tokens for d in drafts) / 1e6 * p_out


def _toml_string(text: str) -> str:
    if "'''" not in text and not text.endswith("'"):
        return "'''\n" + text + "'''" if "\n" in text else "'''" + text + "'''"
    return json.dumps(text)


def to_toml(drafts: list[Draft], model: str) -> str:
    lines = [f"# Drafted by {model} from each measure's description, without seeing its DAX.",
             "# Review every entry. Compare with: dax-parity check --drafts <this file>", ""]
    for d in drafts:
        if d.unclear:
            lines.append(f"# unclear: {' '.join(d.unclear.split())}")
        if d.error:
            lines.append(f"# could not get working SQL: {d.error}")
        for a in d.assumptions:
            lines.append(f"# assumption: {' '.join(a.split())}")
        lines += ["[[measure]]", f"name = {json.dumps(d.measure)}"]
        if d.sql and not d.error:
            lines += [f"sql = {_toml_string(d.sql)}", f'compare = "{d.compare}"']
            if d.blank_if_zero:
                lines.append("blank_if_zero = true")
        lines.append("")
    return "\n".join(lines)


def write(drafts: list[Draft], model: str, path: Path) -> None:
    path.write_text(to_toml(drafts, model), encoding="utf-8")
