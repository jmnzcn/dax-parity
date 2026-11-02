"""What the recorded results depend on, so CI can tell when they are out of date.

Power BI's engine only runs on Windows, so results are recorded there and committed. If someone
then edits a measure, the data or a slice without recording again, comparing the old results would
prove nothing. The fingerprint stores a hash of each measure's DAX, of each reference file and of
each query; `check` refuses stale results and says which part changed.
"""
from __future__ import annotations

from dax_parity import queries, tmdl
from dax_parity.config import Suite


def current(suite: Suite) -> dict:
    project = tmdl.locate(suite.pbip)
    dax = tmdl.measures(project.definition)
    tested = sorted({n for s in suite.slices for n in s.measures})
    return {
        "measures": {n: tmdl.digest(tmdl.normalize(dax[n])) if n in dax else None for n in tested},
        "data": {p.name: tmdl.digest(p.read_bytes()) for p in suite.data_files},
        "queries": {q["id"]: tmdl.digest(q["dax"]) for q in queries.build(suite)},
    }


def stale(recorded: dict, now: dict) -> list[str]:
    reasons = []
    for part, label in (("measures", "measure"), ("data", "data file"), ("queries", "slice")):
        old, new = recorded.get(part, {}), now.get(part, {})
        for name in sorted(set(old) | set(new)):
            if name not in old:
                reasons.append(f"{label} {name!r} was not part of the recording")
            elif old.get(name) != new.get(name):
                reasons.append(f"{label} {name!r} changed since the results were recorded")
    return reasons
