"""One DAX query per slice: SUMMARIZECOLUMNS over the slice's columns, or ROW for the grand total."""
from __future__ import annotations

from dax_parity.config import Slice, Suite


def _measure(name: str) -> str:
    return '"' + name.replace('"', '""') + '", [' + name.replace("]", "]]") + "]"


def dax(slice_: Slice) -> str:
    pairs = ", ".join(_measure(n) for n in slice_.measures)
    if not slice_.by:
        return f"EVALUATE ROW ( {pairs} )"
    group = ", ".join(c.dax for c in slice_.by)
    return f"EVALUATE SUMMARIZECOLUMNS ( {group}, {pairs} )"


def build(suite: Suite) -> list[dict]:
    return [{"id": s.id, "dax": dax(s)} for s in suite.slices]
