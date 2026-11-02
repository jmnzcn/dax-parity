"""dax-parity.toml: which measures to test, on which slices, and how to compute the expected answer."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FILE = "dax-parity.toml"
TOLERANCES = {"exact": 0.0, "money": 0.005, "ratio": 1e-9}
_COLUMN = re.compile(r"^\s*(?:'(?P<q>(?:[^']|'')+)'|(?P<t>[^\[\]']+))\s*\[(?P<c>(?:[^\]]|\]\])+)\]\s*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    table: str
    column: str

    @classmethod
    def parse(cls, ref: str) -> Column:
        m = _COLUMN.match(ref)
        if not m:
            raise ConfigError(f"not a DAX column reference: {ref!r} (expected 'Table'[Column])")
        table = m["q"].replace("''", "'") if m["q"] else m["t"].strip()
        return cls(table, m["c"].replace("]]", "]"))

    @property
    def dax(self) -> str:
        return "'" + self.table.replace("'", "''") + "'[" + self.column.replace("]", "]]") + "]"

    @property
    def key(self) -> str:
        """How Power BI names the column in a query result, and how the reference must name it."""
        return f"{self.table}[{self.column}]"

    @property
    def sql(self) -> str:
        return _ident(self.table) + "." + _ident(self.column)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Measure:
    name: str
    sql: str | None
    tolerance: float
    blank_if_zero: bool


@dataclass(frozen=True)
class Slice:
    id: str
    by: tuple[Column, ...]
    measures: tuple[str, ...]
    sql: str | None = None


@dataclass
class Suite:
    root: Path
    pbip: Path
    parameters: dict[str, str]
    tables: dict[str, Path]
    from_sql: str | None
    python: str | None
    measures: dict[str, Measure]
    slices: list[Slice]
    results: Path
    data_files: list[Path] = field(default_factory=list)
    equivalent: dict[str, str] = field(default_factory=dict)  # mutant id -> why it can't change any result


def load(path: Path, drafts: Path | None = None) -> Suite:
    """Read dax-parity.toml. With drafts, each drafted measure that has SQL replaces the one in the file."""
    path = path.resolve()
    if not path.exists():
        raise ConfigError(f"{path} not found (run `dax-parity init` to create one)")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    if drafts is not None:
        if not drafts.exists():
            raise ConfigError(f"{drafts} not found (run `dax-parity draft` first)")
        drafted = {m["name"]: m for m in tomllib.loads(drafts.read_text(encoding="utf-8")).get("measure", [])
                   if m.get("sql")}
        own = [drafted.pop(m["name"], m) for m in raw.get("measure", [])]
        raw["measure"] = own + list(drafted.values())

    def rel(p: str) -> Path:
        return (root / p.replace("{root}", str(root))).resolve()

    model = raw.get("model", {})
    if "pbip" not in model:
        raise ConfigError("[model] needs pbip = \"path/to/Report.pbip\"")
    ref = raw.get("reference", {})
    tables = {name: rel(p) for name, p in ref.get("tables", {}).items()}
    for name, p in tables.items():
        if not p.exists():
            raise ConfigError(f"reference table {name}: {p} not found")

    measures: dict[str, Measure] = {}
    for m in raw.get("measure", []):
        if "name" not in m:
            raise ConfigError("every [[measure]] needs a name")
        compare = m.get("compare", "ratio")
        if "tolerance" in m:
            tol = float(m["tolerance"])
        elif compare in TOLERANCES:
            tol = TOLERANCES[compare]
        else:
            raise ConfigError(f"measure {m['name']}: compare must be one of {sorted(TOLERANCES)} or set tolerance")
        measures[m["name"]] = Measure(m["name"], m.get("sql"), tol, bool(m.get("blank_if_zero", False)))
    if not measures:
        raise ConfigError("no [[measure]] entries")

    slices = []
    for s in raw.get("slice", [{"id": "totals"}]):
        names = tuple(s.get("measures", [n for n in measures if measures[n].sql or s.get("sql")]))
        unknown = [n for n in names if n not in measures]
        if unknown:
            raise ConfigError(f"slice {s.get('id')}: unknown measures {unknown}")
        if not s.get("sql") and not ref.get("python"):
            missing = [n for n in names if not measures[n].sql]
            if missing:
                raise ConfigError(f"slice {s.get('id')}: measures without sql {missing}; give the slice its own sql")
        slices.append(Slice(s["id"], tuple(Column.parse(c) for c in s.get("by", [])), names, s.get("sql")))
    ids = [s.id for s in slices]
    if len(set(ids)) != len(ids):
        raise ConfigError("slice ids must be unique")

    if tables and not ref.get("from") and any(not s.sql for s in slices) and not ref.get("python"):
        raise ConfigError("[reference] needs from = \"...\" (the joins the generated SQL selects from)")

    equivalent = {}
    for e in raw.get("mutate", {}).get("equivalent", []):
        if not isinstance(e, dict) or "id" not in e or not e.get("reason"):
            raise ConfigError('[[mutate.equivalent]] entries need id = "..." and reason = "..."')
        equivalent[e["id"]] = e["reason"]

    return Suite(
        root=root,
        pbip=rel(model["pbip"]),
        parameters={k: str(v).replace("{root}", str(root)) for k, v in model.get("parameters", {}).items()},
        tables=tables,
        from_sql=ref.get("from"),
        python=ref.get("python"),
        measures=measures,
        slices=slices,
        results=rel(raw.get("results", {}).get("path", "parity/dax_results.json")),
        data_files=sorted(tables.values()),
        equivalent=equivalent,
    )
