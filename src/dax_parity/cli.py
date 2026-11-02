"""dax-parity: check every DAX measure against an independent reference, cell by cell.

  dax-parity init       write a dax-parity.toml listing the model's measures
  dax-parity queries    print the DAX query for each slice
  dax-parity record     run the queries in Power BI Desktop and save the results (Windows)
  dax-parity check      compare the saved results with the reference (anywhere, e.g. CI)
  dax-parity run        record, then check
  dax-parity mutate     break each measure on purpose and count how many breaks the checks catch
  dax-parity validate   validate the model's TMDL with Microsoft's Tabular Object Model
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dax_parity import __version__, compare, config, fingerprint, mutations, queries, reference, tmdl

OK, MISMATCH, ERROR, STALE = 0, 1, 2, 3


def _load(args) -> config.Suite:
    return config.load(Path(args.config))


def cmd_queries(args) -> int:
    for q in queries.build(_load(args)):
        print(f"-- {q['id']}\n{q['dax']}\n")
    return OK


def _engine_args(args) -> dict:
    return {"port": args.port, "refresh": not args.no_refresh, "keep_open": args.keep_open, "timeout": args.timeout}


def cmd_record(args) -> int:
    from dax_parity import desktop

    suite = _load(args)
    results = desktop.record(suite.pbip, suite.parameters, queries.build(suite), **_engine_args(args))
    results = {"tool": f"dax-parity {__version__}", **results, "fingerprint": fingerprint.current(suite)}
    suite.results.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = sum(len(q["rows"]) for q in results["queries"].values())
    print(f"recorded {len(results['queries'])} queries, {rows} rows -> {suite.results}")
    return OK


def cmd_check(args) -> int:
    suite = _load(args)
    if not suite.results.exists():
        print(f"no recorded results at {suite.results}; run `dax-parity record` on Windows first", file=sys.stderr)
        return ERROR
    recorded = json.loads(suite.results.read_text(encoding="utf-8"))
    report = compare.compare(suite, recorded, reference.compute(suite))
    report.stale = fingerprint.stale(recorded.get("fingerprint", {}), fingerprint.current(suite))
    print(compare.render(report))
    if args.junit:
        Path(args.junit).write_text(compare.junit(report), encoding="utf-8")
    if report.stale:
        print("\nthe recorded results are out of date:")
        print("\n".join(f"  {r}" for r in report.stale))
        print("record again with `dax-parity record`" + (" (ignored: --allow-stale)" if args.allow_stale else ""))
        if not args.allow_stale:
            return STALE
    return OK if report.ok else MISMATCH


def cmd_run(args) -> int:
    code = cmd_record(args)
    return code if code else cmd_check(args)


def cmd_mutate(args) -> int:
    from dax_parity import desktop, mutate

    suite = _load(args)
    operators = set(args.operators.split(",")) if args.operators else None
    unknown = (operators or set()) - set(mutate.mutations.OPERATORS)
    if unknown:
        print(f"unknown operators {sorted(unknown)}; choose from {', '.join(mutate.mutations.OPERATORS)}", file=sys.stderr)
        return ERROR
    jobs, untested = mutate.plan(suite, operators, set(args.measure) if args.measure else None)
    expected = reference.compute(suite)
    print(f"{len(jobs) - 1} mutants to run, {len(untested)} on measures no slice compares", file=sys.stderr)
    results = desktop.mutate(suite.pbip, suite.parameters, mutate.wire(jobs), **_engine_args(args))
    report = mutate.evaluate(suite, jobs, results, expected, untested)
    print(mutate.render(report))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(mutate.to_json(report), encoding="utf-8")
    if args.min_score is not None and (report.score or 0) < args.min_score:
        print(f"\nmutation score below {args.min_score:.0%}")
        return MISMATCH
    return OK


def cmd_validate(args) -> int:
    from dax_parity import desktop

    suite = _load(args)
    project = tmdl.locate(suite.pbip)
    done = __import__("subprocess").run([*desktop.runner(), "validate", str(project.definition)])
    return OK if done.returncode == 0 else MISMATCH


def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return ERROR
    pbip = Path(args.pbip)
    project = tmdl.locate(pbip)
    lines = ["[model]", f'pbip = "{pbip.as_posix()}"']
    params = tmdl.parameters(project.definition)
    if params:
        lines.append("# parameters the scratch copy gets before Power BI opens it; {root} is this file's folder")
        lines.append("parameters = { " + ", ".join(f'{k} = "{v}"' for k, v in params.items()).replace("\\", "/") + " }")
    tables = sorted(p.stem for p in (project.definition / "tables").glob("*.tmdl"))
    lines += ["", "[reference]", "# the files each table is loaded from (parquet, csv or json)", "tables = { "
              + ", ".join(f'{t} = "data/{t.lower()}.parquet"' for t in tables) + " }",
              '# the joins every generated query selects from', f'from = "{tables[0] if tables else "Fact"}"', ""]
    for name in tmdl.measures(project.definition):
        lines += ["[[measure]]", f'name = "{name}"', 'sql = ""  # e.g. SUM("Sales"."Amount")',
                  'compare = "ratio"  # exact, money (0.005) or ratio (1e-9); or tolerance = ...', ""]
    lines += ["[[slice]]", 'id = "totals"', "", "# [[slice]]", '# id = "by_category"', "# by = [\"'Product'[Category]\"]", ""]
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {target}: fill in each measure's sql and the slices you want")
    return OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dax-parity", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default=config.DEFAULT_FILE, help="path to dax-parity.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="write a dax-parity.toml from a .pbip")
    i.add_argument("pbip")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)

    sub.add_parser("queries", help="print the DAX queries").set_defaults(fn=cmd_queries)

    helps = {"record": "run the queries in Power BI Desktop", "run": "record, then check",
             "mutate": "break each measure on purpose and count how many breaks the checks catch"}
    for name, fn in (("record", cmd_record), ("run", cmd_run), ("mutate", cmd_mutate)):
        r = sub.add_parser(name, help=helps[name])
        r.add_argument("--port", type=int, help="use a Power BI Desktop that is already open on this port")
        r.add_argument("--no-refresh", action="store_true", help="query the data already loaded")
        r.add_argument("--keep-open", action="store_true", help="leave Power BI Desktop open afterwards")
        r.add_argument("--timeout", type=float, default=300)
        if name == "run":
            r.add_argument("--junit")
            r.add_argument("--allow-stale", action="store_true")
        if name == "mutate":
            r.add_argument("--operators", help="comma-separated subset of: " + ", ".join(mutations.OPERATORS))
            r.add_argument("--measure", action="append", help="only mutate this measure (repeatable)")
            r.add_argument("--json", help="write every mutant and its outcome here")
            r.add_argument("--min-score", type=float, help="exit with 1 if the score is below this (0 to 1)")
        r.set_defaults(fn=fn)

    c = sub.add_parser("check", help="compare recorded results with the reference")
    c.add_argument("--junit", help="also write a JUnit XML report here")
    c.add_argument("--allow-stale", action="store_true", help="compare even if the results are out of date")
    c.set_defaults(fn=cmd_check)

    sub.add_parser("validate", help="validate the TMDL with Microsoft's Tabular Object Model").set_defaults(fn=cmd_validate)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return ERROR
    except Exception as e:  # noqa: BLE001 - one clear line instead of a traceback for expected failures
        from dax_parity.desktop import DesktopError
        if isinstance(e, DesktopError | FileNotFoundError | ValueError):
            print(f"error: {e}", file=sys.stderr)
            return ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
