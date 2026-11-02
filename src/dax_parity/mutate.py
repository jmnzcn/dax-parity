"""Mutation testing: how many deliberately broken measures would the parity checks catch?

For every mutant of every measure, the model is changed in Power BI's engine, only the queries that
can see the change are run (the mutated measure and every tested measure built on it), and the
results are compared with the reference. A mutant is:

- killed: at least one compared cell changed, so the checks would have caught that bug;
- survived: no compared cell changed, so a bug like it would ship unnoticed;
- invalid: the engine rejected the changed DAX, so it says nothing about the checks;
- untested: neither the measure nor anything built on it is compared on any slice;
- equivalent: it survived, and dax-parity.toml says why it can never change a result (for example an
  ALL on a table that the model already ignores filters on). Listed, but not scored.

The mutation score is killed / (killed + survived + untested).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from dax_parity import compare, mutations, queries, tmdl
from dax_parity.config import Slice, Suite

BASELINE = "(unchanged model)"

ADVICE = {
    "drop_filter": "group a slice by a column this filter or ALL touches; if nothing can show it, the filter may be "
                   "redundant (mark it equivalent in dax-parity.toml, or remove it)",
    "comparison": "the boundary is never hit: add a slice at the grain where values sit exactly on it (often by day)",
    "aggregation": "every group has one row, or the same value, so the aggregations agree; a coarser slice would differ",
    "blank": "no slice has a group where this measure is BLANK; group by something that leaves some groups empty",
    "negate_if": "no tested slice reaches the other branch of the IF",
    "constant": "this number doesn't change any tested cell; is it used at all?",
    "string": "this text value never matches rows in the tested slices",
    "boolean": "the flag is never true (or never false) in the tested data",
}


@dataclass
class Outcome:
    measure: str
    operator: str
    description: str
    status: str
    expression: str
    mismatches: int = 0
    example: str = ""
    error: str = ""
    reason: str = ""
    seconds: float = 0.0

    @property
    def id(self) -> str:
        return f"{self.measure} / {self.operator} / {self.description}"


@dataclass
class MutationReport:
    outcomes: list[Outcome] = field(default_factory=list)
    equivalent_ids: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(o.status == status for o in self.outcomes)

    @property
    def score(self) -> float | None:
        scored = self.count("killed") + self.count("survived") + self.count("untested")
        return self.count("killed") / scored if scored else None


def _subset(slice_: Slice, names: set[str]) -> Slice:
    return Slice(slice_.id, slice_.by, tuple(n for n in slice_.measures if n in names), slice_.sql)


def _project(rows: list[dict], slice_: Slice) -> list[dict]:
    """Expected rows for a slice that queries only some of its measures (SUMMARIZECOLUMNS drops all-BLANK rows)."""
    keys = [c.key for c in slice_.by]
    out = []
    for r in rows:
        if any(r.get(n) is not None for n in slice_.measures):
            out.append({k: r[k] for k in keys} | {n: r.get(n) for n in slice_.measures})
    return out


def plan(suite: Suite, operators: set[str] | None = None, only: set[str] | None = None):
    """Jobs for the runner (the baseline first), and the mutants no slice can see."""
    dax = tmdl.measures(tmdl.locate(suite.pbip).definition)
    tested = {n for s in suite.slices for n in s.measures}
    missing = tested - set(dax)
    if missing:
        raise ValueError(f"measures in dax-parity.toml but not in the model: {sorted(missing)}")
    deps = mutations.dependents(dax)
    jobs = [{"id": BASELINE, "queries": queries.build(suite), "slices": suite.slices}]
    untested = []
    for name, expression in dax.items():
        if only and name not in only:
            continue
        affected = deps[name] & tested
        for m in mutations.mutants(name, expression, operators):
            if not affected:
                untested.append(m)
                continue
            slices = [_subset(s, affected) for s in suite.slices if affected & set(s.measures)]
            jobs.append({"id": m.id, "measure": name, "expression": m.expression, "mutant": m, "slices": slices,
                         "queries": [{"id": s.id, "dax": queries.dax(s)} for s in slices]})
    return jobs, untested


def wire(jobs: list[dict]) -> list[dict]:
    """What the .NET runner needs from each job."""
    return [{k: v for k, v in j.items() if k in ("id", "measure", "expression", "queries")} for j in jobs]


def evaluate(suite: Suite, jobs: list[dict], results: list[dict], expected: dict, untested: list) -> MutationReport:
    by_id = {r["id"]: r for r in results}
    base = by_id.get(BASELINE, {})
    if base.get("error"):
        raise ValueError(f"the unchanged model failed to run: {base['error']}")
    baseline = compare.compare(suite, base, expected)
    if not baseline.ok:
        raise ValueError(f"the unchanged model already differs from the reference in {len(baseline.mismatches)} "
                         "cells; fix that first (dax-parity check), mutation testing assumes a passing baseline")
    report = MutationReport(equivalent_ids=list(suite.equivalent))
    for job in jobs[1:]:
        m: mutations.Mutant = job["mutant"]
        r = by_id.get(job["id"], {"error": "no result"})
        o = Outcome(m.measure, m.operator, m.description, "", m.expression, seconds=r.get("seconds", 0.0))
        if r.get("error"):
            o.status, o.error = "invalid", r["error"]
        else:
            sub_expected = {s.id: _project(expected[s.id], s) for s in job["slices"]}
            result = compare.compare(suite, r, sub_expected, job["slices"])
            o.mismatches = len(result.mismatches)
            o.status = "killed" if result.mismatches else "survived"
            if result.mismatches:
                o.example = result.mismatches[0].describe()
            elif o.id in suite.equivalent:
                o.status, o.reason = "equivalent", suite.equivalent[o.id]
        report.outcomes.append(o)
    for m in untested:
        report.outcomes.append(Outcome(m.measure, m.operator, m.description, "untested", m.expression))
    return report


def render(report: MutationReport, show: int = 25) -> str:
    score = report.score
    lines = [
        f"mutation score: {'n/a' if score is None else f'{score:.0%}'}  "
        f"({report.count('killed')} killed, {report.count('survived')} survived, {report.count('untested')} untested, "
        f"{report.count('equivalent')} equivalent, {report.count('invalid')} invalid)",
        "",
        "by measure:",
    ]
    names = list(dict.fromkeys(o.measure for o in report.outcomes))
    width = max((len(n) for n in names), default=10)
    for name in names:
        mine = [o for o in report.outcomes if o.measure == name]
        killed = sum(o.status == "killed" for o in mine)
        scored = sum(o.status in ("killed", "survived", "untested") for o in mine)
        flag = "" if killed == scored else "  <-"
        lines.append(f"  {name:<{width}}  {killed:3d} of {scored:3d} caught{flag}")
    alive = [o for o in report.outcomes if o.status in ("survived", "untested")]
    if alive:
        lines += ["", "bugs the checks would miss:"]
        for o in alive[:show]:
            tag = "not compared on any slice" if o.status == "untested" else ADVICE.get(o.operator, "")
            lines.append(f"  {o.measure}: {o.description}")
            lines.append(f"      {' '.join(o.expression.split())[:140]}")
            if tag:
                lines.append(f"      -> {tag}")
        if len(alive) > show:
            lines.append(f"  ... and {len(alive) - show} more (see --json)")
    equivalent = [o for o in report.outcomes if o.status == "equivalent"]
    if equivalent:
        lines += ["", "marked equivalent in dax-parity.toml (not scored):"]
        for o in equivalent:
            lines += [f"  {o.measure}: {o.description}", f"      -> {o.reason}"]
    marked = {o.id for o in equivalent}
    killed_but_marked = [i for i in report.equivalent_ids if i not in marked]
    if killed_but_marked:
        lines += ["", "marked equivalent but no longer surviving (remove from dax-parity.toml):"]
        lines += [f"  {i}" for i in killed_but_marked]
    invalid = [o for o in report.outcomes if o.status == "invalid"]
    if invalid:
        lines += ["", f"{len(invalid)} mutants were rejected by the engine (not counted):"]
        lines += [f"  {o.measure}: {o.description}: {o.error[:100]}" for o in invalid[:5]]
    return "\n".join(lines)


def to_json(report: MutationReport) -> str:
    return json.dumps({"score": report.score, "outcomes": [asdict(o) for o in report.outcomes]}, indent=1,
                      ensure_ascii=False) + "\n"
