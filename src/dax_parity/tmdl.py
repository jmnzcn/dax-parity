"""Just enough of a Power BI project to test it: where the semantic model is, its measures, its parameters.

This is not a TMDL parser. It reads the three things dax-parity needs and nothing else; the full
model is validated with Microsoft's own serializer by `dax-parity validate`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_MEASURE = re.compile(r"^(?P<indent>\t+)measure\s+(?P<name>'(?:[^']|'')+'|[^\s=]+)\s*=\s*(?P<rest>.*)$")
_EXPRESSION = re.compile(r'^expression\s+(?P<name>\'(?:[^\']|\'\')+\'|[^\s=]+)\s*=\s*"(?P<value>(?:[^"]|"")*)"')


def _unquote(name: str) -> str:
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


@dataclass(frozen=True)
class Project:
    pbip: Path
    report: Path
    semantic_model: Path

    @property
    def definition(self) -> Path:
        return self.semantic_model / "definition"


def locate(pbip: Path) -> Project:
    """Follow the .pbip to its report, and the report to its semantic model."""
    meta = json.loads(pbip.read_text(encoding="utf-8-sig"))
    reports = [a["report"]["path"] for a in meta.get("artifacts", []) if "report" in a]
    if not reports:
        raise ValueError(f"{pbip} lists no report")
    report = (pbip.parent / reports[0]).resolve()
    pbir = json.loads((report / "definition.pbir").read_text(encoding="utf-8-sig"))
    by_path = pbir.get("datasetReference", {}).get("byPath", {}).get("path")
    if not by_path:
        raise ValueError(f"{report / 'definition.pbir'} does not point to a local semantic model")
    return Project(pbip.resolve(), report, (report / by_path).resolve())


@dataclass(frozen=True)
class MeasureInfo:
    name: str
    table: str
    dax: str
    description: str
    format: str
    folder: str


def measures(definition: Path) -> dict[str, str]:
    """Every measure's DAX expression, keyed by measure name."""
    return {name: info.dax for name, info in measure_info(definition).items()}


def measure_info(definition: Path) -> dict[str, MeasureInfo]:
    """Every measure with its expression, its /// description, format string and display folder."""
    out: dict[str, MeasureInfo] = {}
    for path in sorted((definition / "tables").glob("*.tmdl")):
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        i = 0
        while i < len(lines):
            m = _MEASURE.match(lines[i])
            i += 1
            if not m:
                continue
            depth = len(m["indent"])
            doc, j = [], i - 2
            while j >= 0 and lines[j].strip().startswith("///"):
                doc.insert(0, lines[j].strip()[3:].strip())
                j -= 1
            rest = m["rest"].strip()
            if rest.startswith("```"):  # fenced expression
                body = [rest[3:]] if rest[3:] else []
                while i < len(lines) and lines[i].strip() != "```":
                    body.append(lines[i])
                    i += 1
                i += 1
            else:
                body = [rest] if rest else []
                # Continuation lines sit deeper than the measure's own properties.
                while i < len(lines) and (not lines[i].strip() or _depth(lines[i]) > depth + 1):
                    body.append(lines[i])
                    i += 1
            props = {}
            while i < len(lines) and lines[i].strip() and _depth(lines[i]) == depth + 1 and ":" in lines[i]:
                key, _, value = lines[i].strip().partition(":")
                props[key.strip()] = value.strip()
                i += 1
            name = _unquote(m["name"])
            out[name] = MeasureInfo(name, path.stem, "\n".join(b.strip() for b in body if b.strip()),
                                    " ".join(doc), props.get("formatString", ""), props.get("displayFolder", ""))
    return out


def _depth(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def parameters(definition: Path) -> dict[str, str]:
    path = definition / "expressions.tmdl"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = _EXPRESSION.match(line)
        if m and "IsParameterQuery=true" in line:
            out[_unquote(m["name"])] = m["value"].replace('""', '"')
    return out


def set_parameters(definition: Path, values: dict[str, str]) -> None:
    """Rewrite parameter values in place (used on a scratch copy, never on the user's project)."""
    path = definition / "expressions.tmdl"
    text = path.read_text(encoding="utf-8-sig")
    for name, value in values.items():
        pattern = re.compile(r'^(expression\s+' + re.escape(name) + r"\s*=\s*)\"(?:[^\"]|\"\")*\"", re.M)
        quoted = value.replace('"', '""')
        text, n = pattern.subn(lambda m, q=quoted: f'{m[1]}"{q}"', text)
        if n != 1:
            raise ValueError(f"parameter {name!r} not found in {path}")
    path.write_text(text, encoding="utf-8")


def normalize(dax: str) -> str:
    return " ".join(dax.split())


def digest(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:16]
