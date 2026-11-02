"""The expected answer, computed without DAX.

By default each measure is a SQL aggregate run by DuckDB over the same files the model loads, and
dax-parity writes the GROUP BY for every slice. A slice can bring its own SQL (running totals,
anything that needs a window), and `[reference] python = "module:function"` hands the whole thing
to your own code.
"""
from __future__ import annotations

import datetime as dt
import decimal
import importlib
import sys
from collections.abc import Iterable

from dax_parity.config import ConfigError, Slice, Suite, _ident

Rows = list[dict]


def _scan(path) -> str:
    p = str(path).replace("'", "''")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return f"read_parquet('{p}')"
    if suffix in (".csv", ".txt"):
        return f"read_csv_auto('{p}')"
    if suffix == ".json":
        return f"read_json_auto('{p}')"
    raise ConfigError(f"{path}: reference tables must be .parquet, .csv or .json")


def sql_for(suite: Suite, slice_: Slice) -> str:
    if slice_.sql:
        return slice_.sql
    cols = [f"{c.sql} AS {_ident(c.key)}" for c in slice_.by]
    cols += [f"({suite.measures[n].sql}) AS {_ident(n)}" for n in slice_.measures]
    q = f"SELECT {', '.join(cols)}\nFROM {suite.from_sql}"
    if slice_.by:
        q += "\nGROUP BY " + ", ".join(c.sql for c in slice_.by)
    return q


def value(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def _finish(suite: Suite, slice_: Slice, rows: Iterable[dict]) -> Rows:
    out = []
    for r in rows:
        r = {k: value(v) for k, v in r.items()}
        for name in slice_.measures:
            if name not in r:
                raise ConfigError(f"slice {slice_.id}: reference has no column {name!r}")
            if suite.measures[name].blank_if_zero and r[name] == 0:
                r[name] = None
        # SUMMARIZECOLUMNS drops a row when every measure in it is BLANK; so must the reference.
        if any(r[n] is not None for n in slice_.measures):
            out.append(r)
    return out


def compute(suite: Suite) -> dict[str, Rows]:
    if suite.python:
        return _python(suite)
    import duckdb

    con = duckdb.connect()
    for name, path in suite.tables.items():
        con.execute(f"CREATE VIEW {_ident(name)} AS SELECT * FROM {_scan(path)}")
    out = {}
    for s in suite.slices:
        try:
            cur = con.execute(sql_for(suite, s))
        except duckdb.Error as e:
            raise ConfigError(f"slice {s.id}: the reference SQL failed: {e}") from e
        names = [d[0] for d in cur.description]
        missing = [c.key for c in s.by if c.key not in names]
        if missing:
            raise ConfigError(f"slice {s.id}: reference SQL must return columns named {missing}")
        out[s.id] = _finish(suite, s, (dict(zip(names, row, strict=True)) for row in cur.fetchall()))
    return out


def _python(suite: Suite) -> dict[str, Rows]:
    module, _, func = suite.python.partition(":")
    if str(suite.root) not in sys.path:
        sys.path.insert(0, str(suite.root))
    fn = getattr(importlib.import_module(module), func)
    result = fn(suite)
    out = {}
    for s in suite.slices:
        rows = result[s.id]
        if hasattr(rows, "to_dict"):  # a pandas DataFrame
            rows = rows.astype(object).where(rows.notna(), None).to_dict("records")
        out[s.id] = _finish(suite, s, rows)
    return out
