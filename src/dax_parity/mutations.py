"""Small, deliberate bugs in DAX measures, to find out whether the parity checks would notice them.

Each operator makes one change that a person could plausibly make by mistake: the wrong aggregation,
a flipped comparison, a filter dropped from CALCULATE, a missing "+ 0". A mutant that changes no
compared cell "survives", which means the slices you test can't tell that measure apart from a
broken version of it.

The changes are made on tokens, so text inside strings, table names, column names and comments is
never touched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"""
  (?P<ws>\s+)
| (?P<comment>//[^\n]*|--[^\n]*|/\*.*?\*/)
| (?P<string>"(?:[^"]|"")*")
| (?P<qname>'(?:[^']|'')*')
| (?P<bracket>\[(?:[^\]]|\]\])*\])
| (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
| (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
| (?P<op><=|>=|<>|==|&&|\|\||[-+*/^&=<>(),{}])
| (?P<other>.)
""", re.S | re.X)

AGGREGATIONS = {
    "SUM": "AVERAGE", "AVERAGE": "SUM", "MIN": "MAX", "MAX": "MIN", "DISTINCTCOUNT": "COUNT",
    "SUMX": "AVERAGEX", "AVERAGEX": "SUMX", "MINX": "MAXX", "MAXX": "MINX",
}
COMPARISONS = {"=": "<>", "==": "<>", "<>": "=", ">": ">=", ">=": ">", "<": "<=", "<=": "<"}
ARITHMETIC = {"+": "-", "-": "+", "*": "/", "/": "*"}
FILTER_FUNCTIONS = {"CALCULATE", "CALCULATETABLE"}

OPERATORS = {
    "aggregation": "swap an aggregation (SUM for AVERAGE, MIN for MAX, DISTINCTCOUNT for COUNT)",
    "comparison": "flip a comparison (= for <>, > for >=, < for <=)",
    "logical": "swap && and ||",
    "arithmetic": "swap + and -, * and /",
    "constant": "change a number (n becomes n + 1)",
    "boolean": "swap TRUE () and FALSE ()",
    "string": "change a text literal",
    "drop_filter": "remove one filter argument from CALCULATE",
    "negate_if": "negate the condition of an IF",
    "blank": "remove a '+ 0' or COALESCE ( ..., 0 ) that turns BLANK into 0",
    "divide": "swap the numerator and denominator of DIVIDE",
}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Mutant:
    measure: str
    operator: str
    description: str
    original: str
    expression: str

    @property
    def id(self) -> str:
        return f"{self.measure} / {self.operator} / {self.description}"


def tokenize(dax: str) -> list[Token]:
    """Significant tokens only (no whitespace or comments), with their position in the text."""
    out = []
    for m in _TOKEN.finditer(dax):
        if m.lastgroup not in ("ws", "comment"):
            out.append(Token(m.lastgroup, m[0], m.start(), m.end()))
    return out


def _is_call(tokens: list[Token], i: int) -> bool:
    return tokens[i].kind == "ident" and i + 1 < len(tokens) and tokens[i + 1].text == "("


def _close(tokens: list[Token], open_i: int) -> int:
    depth = 0
    for j in range(open_i, len(tokens)):
        if tokens[j].text in ("(", "{"):
            depth += 1
        elif tokens[j].text in (")", "}"):
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _args(tokens: list[Token], call_i: int) -> list[tuple[int, int]]:
    """Token index ranges [first, last] of each top-level argument of the call starting at call_i."""
    open_i = call_i + 1
    close_i = _close(tokens, open_i)
    args, depth, first = [], 0, open_i + 1
    for j in range(open_i + 1, close_i):
        t = tokens[j].text
        if t in ("(", "{"):
            depth += 1
        elif t in (")", "}"):
            depth -= 1
        elif t == "," and depth == 0:
            args.append((first, j - 1))
            first = j + 1
    if first <= close_i - 1:
        args.append((first, close_i - 1))
    return args


def _operand_before(tokens: list[Token], i: int) -> bool:
    """Whether the token before i ends an operand, so the operator at i is binary."""
    return i > 0 and (tokens[i - 1].kind in ("number", "string", "bracket", "qname", "ident")
                      or tokens[i - 1].text in (")", "}"))


def _left(tokens: list[Token], i: int) -> int:
    """First token of the operand just before position i."""
    j = i - 1
    if j < 0:
        return i
    if tokens[j].text == ")":
        depth = 0
        while j >= 0:
            depth += {")": 1, "(": -1}.get(tokens[j].text, 0)
            if depth == 0:
                break
            j -= 1
        return j - 1 if j > 0 and tokens[j - 1].kind == "ident" else j
    if tokens[j].kind == "bracket" and j > 0 and tokens[j - 1].end == tokens[j].start:
        return j - 1
    return j


def _right(tokens: list[Token], i: int) -> int:
    """Last token of the operand just after position i."""
    j = i + 1
    if j >= len(tokens):
        return i
    if _is_call(tokens, j):
        return _close(tokens, j + 1)
    if j + 1 < len(tokens) and tokens[j + 1].kind == "bracket" and tokens[j].end == tokens[j + 1].start:
        return j + 1
    return j


def _is_var_assignment(tokens: list[Token], i: int) -> bool:
    return i >= 2 and tokens[i - 2].kind == "ident" and tokens[i - 2].text.upper() == "VAR"


def mutants(name: str, dax: str, operators: set[str] | None = None) -> list[Mutant]:
    tokens = tokenize(dax)
    edits: list[tuple[str, str, int, int, str]] = []  # operator, description, start, end, replacement

    def add(op: str, desc: str, start: int, end: int, text: str) -> None:
        if operators is None or op in operators:
            edits.append((op, desc, start, end, text))

    for i, t in enumerate(tokens):
        up = t.text.upper()
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if _is_call(tokens, i) and up in AGGREGATIONS:
            call = dax[t.start:tokens[_close(tokens, i + 1)].end]
            add("aggregation", f"{_short(call)} -> {AGGREGATIONS[up]}", t.start, t.end, AGGREGATIONS[up])
        if t.kind == "op" and t.text in COMPARISONS and not _is_var_assignment(tokens, i):
            near = _short(dax[tokens[_left(tokens, i)].start:tokens[_right(tokens, i)].end], 50)
            add("comparison", f"{t.text} -> {COMPARISONS[t.text]} in {near}", t.start, t.end, COMPARISONS[t.text])
        if t.kind == "op" and t.text in ("&&", "||"):
            other = "||" if t.text == "&&" else "&&"
            add("logical", f"{t.text} -> {other}", t.start, t.end, other)
        if t.kind == "op" and t.text in ARITHMETIC and _operand_before(tokens, i):
            zero = nxt is not None and nxt.kind == "number" and float(nxt.text) == 0
            if zero and t.text in ("+", "-"):
                if t.text == "+":
                    add("blank", "remove + 0", tokens[i - 1].end, nxt.end, "")
            else:
                add("arithmetic", f"{t.text} -> {ARITHMETIC[t.text]}", t.start, t.end, ARITHMETIC[t.text])
        if t.kind == "number" and not (i > 0 and tokens[i - 1].text in ("+", "-") and float(t.text) == 0):
            n = float(t.text)
            new = str(int(n) + 1) if n.is_integer() and "." not in t.text else repr(n * 2)
            add("constant", f"{t.text} -> {new}", t.start, t.end, new)
        if up in ("TRUE", "FALSE") and nxt is not None and nxt.text == "(":
            other = "FALSE" if up == "TRUE" else "TRUE"
            add("boolean", f"{up} () -> {other} ()", t.start, t.end, other)
        if t.kind == "string":
            add("string", f"{t.text} -> \"\"", t.start, t.end, '"__mutant__"')
        if _is_call(tokens, i) and up in FILTER_FUNCTIONS:
            args = _args(tokens, i)
            for a, b in args[1:]:
                prev_comma = tokens[a - 1]
                text = dax[tokens[a].start:tokens[b].end]
                add("drop_filter", f"drop {_short(text)}", prev_comma.start, tokens[b].end, "")
        if _is_call(tokens, i) and up == "IF":
            args = _args(tokens, i)
            if args:
                a, b = args[0]
                cond = dax[tokens[a].start:tokens[b].end]
                add("negate_if", f"IF ( {_short(cond)} ) negated", tokens[a].start, tokens[b].end, f"NOT ( {cond} )")
        if _is_call(tokens, i) and up == "DIVIDE":
            args = _args(tokens, i)
            if len(args) >= 2:
                (a1, b1), (a2, b2) = args[0], args[1]
                num, den = dax[tokens[a1].start:tokens[b1].end], dax[tokens[a2].start:tokens[b2].end]
                add("divide", f"DIVIDE ( {_short(den, 20)}, {_short(num, 20)} )", tokens[a1].start, tokens[b2].end,
                    f"{den}, {num}")
        if _is_call(tokens, i) and up == "COALESCE":
            args = _args(tokens, i)
            if len(args) == 2:
                a, b = args[0]
                inner = dax[tokens[a].start:tokens[b].end]
                add("blank", "remove COALESCE", t.start, tokens[_close(tokens, i + 1)].end, inner)

    out, seen = [], set()
    for op, desc, start, end, text in edits:
        expression = dax[:start] + text + dax[end:]
        if expression == dax or expression in seen:
            continue
        seen.add(expression)
        out.append(Mutant(name, op, desc, dax, expression))
    return out


def _short(text: str, n: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def references(dax: str) -> set[str]:
    """Measures a DAX expression refers to: [Name] on its own, not Table[Column]."""
    tokens = tokenize(dax)
    out = set()
    for i, t in enumerate(tokens):
        if t.kind != "bracket":
            continue
        prev = tokens[i - 1] if i else None
        if prev is not None and prev.end == t.start and prev.kind in ("ident", "qname"):
            continue  # a column: Table[Column] or 'Table'[Column]
        out.add(t.text[1:-1].replace("]]", "]"))
    return out


def dependents(measures: dict[str, str]) -> dict[str, set[str]]:
    """For each measure, every measure whose value depends on it (directly or through others), itself included."""
    refs = {name: references(dax) & set(measures) for name, dax in measures.items()}
    out = {}
    for name in measures:
        found, frontier = {name}, [name]
        while frontier:
            current = frontier.pop()
            for other, uses in refs.items():
                if current in uses and other not in found:
                    found.add(other)
                    frontier.append(other)
        out[name] = found
    return out
