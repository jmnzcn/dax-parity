"""Cell-by-cell comparison of what Power BI returned with what the reference expects.

Every difference gets a kind, because the kind usually says what to fix:

- blank_vs_zero: one side is BLANK, the other is 0. Classic DAX: DIVIDE(BLANK, n) and CALCULATE
  over no rows are BLANK, so a visual shows an empty cell where the answer is 0 (or the reverse).
- missing_row: the reference has a row Power BI didn't return (a filter or relationship drops it).
- extra_row: Power BI returned a row the reference doesn't have.
- value: both sides have a number and they differ by more than the measure's tolerance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from dax_parity.config import Suite
from dax_parity.reference import value

HINTS = {
    "blank_vs_zero": "BLANK and 0 differ. If the answer should be 0, add + 0 or COALESCE ( ..., 0 ) to the DAX; "
                     "if it should be blank, set blank_if_zero = true on the measure.",
    "missing_row": "Power BI did not return this row. Check relationships, filters and whether every measure "
                   "in the slice is BLANK there.",
    "extra_row": "Power BI returned a row the reference doesn't have. Check the reference's joins and filters.",
}


@dataclass
class Mismatch:
    slice: str
    key: tuple
    measure: str
    dax: object
    expected: object
    kind: str

    def describe(self) -> str:
        where = ", ".join(map(str, self.key)) if self.key else "total"
        return f"{self.slice} [{where}] {self.measure}: Power BI {_fmt(self.dax)}, expected {_fmt(self.expected)}"


def _fmt(v) -> str:
    return "BLANK" if v is None else repr(v)


@dataclass
class Report:
    cells: int = 0
    slices: dict[str, dict] = field(default_factory=dict)
    by_measure: dict[str, dict[str, int]] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    engine: str | None = None
    recorded_utc: str | None = None

    @property
    def ok(self) -> bool:
        return not self.mismatches


def clean_key(name: str) -> str:
    """Power BI names group columns 'Table[Column]' and measures '[Measure]'."""
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


def _key(v):
    v = value(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _num(v):
    if v is None or isinstance(v, bool):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _kind(got, exp, tol: float) -> str | None:
    got, exp = _num(got), _num(exp)
    if got is None or exp is None:
        if got is None and exp is None:
            return None
        other = exp if got is None else got
        return "blank_vs_zero" if other == 0 else "value"
    if isinstance(got, float) and isinstance(exp, float):
        return None if math.isclose(got, exp, rel_tol=0, abs_tol=tol) or got == exp else "value"
    return None if got == exp else "value"


def compare(suite: Suite, recorded: dict, expected: dict[str, list[dict]]) -> Report:
    report = Report(engine=recorded.get("engine"), recorded_utc=recorded.get("recorded_utc"))
    for s in suite.slices:
        keys = [c.key for c in s.by]
        got_rows = recorded.get("queries", {}).get(s.id, {}).get("rows", [])
        got = {}
        for r in got_rows:
            r = {clean_key(k): v for k, v in r.items()}
            got[tuple(_key(r.get(k)) for k in keys)] = r
        exp = {tuple(_key(r[k]) for k in keys): r for r in expected[s.id]}
        stats = {"rows": len(exp), "cells": 0, "mismatches": 0}
        for key in list(exp) + [k for k in got if k not in exp]:
            e, g = exp.get(key), got.get(key)
            for name in s.measures:
                ev = e.get(name) if e else None
                gv = g.get(name) if g else None
                if e is None and gv is None:
                    continue
                stats["cells"] += 1
                if e is None:
                    kind = "extra_row"
                elif g is None:
                    kind = "missing_row" if ev is not None else None
                else:
                    kind = _kind(gv, ev, suite.measures[name].tolerance)
                m = report.by_measure.setdefault(name, {"cells": 0, "mismatches": 0})
                m["cells"] += 1
                if kind:
                    m["mismatches"] += 1
                    stats["mismatches"] += 1
                    report.mismatches.append(Mismatch(s.id, key, name, gv, ev, kind))
        report.slices[s.id] = stats
        report.cells += stats["cells"]
    return report


def render(report: Report, limit: int = 15) -> str:
    lines = []
    if report.engine:
        lines.append(f"recorded with {report.engine} at {report.recorded_utc}")
    width = max((len(s) for s in report.slices), default=10)
    for sid, s in report.slices.items():
        lines.append(f"  {sid:<{width}}  {s['rows']:6d} rows  {s['cells']:7d} cells  {s['mismatches']:5d} mismatches")
    lines.append(f"{report.cells} cells compared, {len(report.mismatches)} mismatches")
    if report.mismatches:
        lines.append("")
        lines.append("by measure:")
        for name, m in sorted(report.by_measure.items(), key=lambda kv: -kv[1]["mismatches"]):
            if m["mismatches"]:
                kinds = {}
                for x in report.mismatches:
                    if x.measure == name:
                        kinds[x.kind] = kinds.get(x.kind, 0) + 1
                detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
                lines.append(f"  {name}: {m['mismatches']} of {m['cells']} cells ({detail})")
        lines.append("")
        lines.append("first differences:")
        lines += [f"  {m.describe()}  [{m.kind}]" for m in report.mismatches[:limit]]
        seen = []
        for m in report.mismatches:
            if m.kind in HINTS and m.kind not in seen:
                seen.append(m.kind)
        if seen:
            lines.append("")
            lines += [f"hint ({k}): {HINTS[k]}" for k in seen]
    return "\n".join(lines)


def junit(report: Report) -> str:
    """One test case per measure and slice, so CI systems show exactly which measure broke where."""
    cases, failures = [], 0
    by = {}
    for m in report.mismatches:
        by.setdefault((m.slice, m.measure), []).append(m)
    names = sorted({(sid, m) for sid in report.slices for m in report.by_measure})
    for sid, measure in names:
        bad = by.get((sid, measure), [])
        body = ""
        if bad:
            failures += 1
            text = "\n".join(x.describe() + f" [{x.kind}]" for x in bad[:50])
            body = f"<failure message={quoteattr(f'{len(bad)} cells differ')}>{escape(text)}</failure>"
        cases.append(f'<testcase classname={quoteattr("dax-parity." + sid)} name={quoteattr(measure)}>{body}</testcase>')
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<testsuite name="dax-parity" tests="{len(cases)}" failures="{failures}">\n'
            + "\n".join(cases) + "\n</testsuite>\n")
