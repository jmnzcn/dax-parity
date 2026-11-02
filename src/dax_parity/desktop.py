"""Running the queries inside Power BI Desktop (Windows only).

`record` copies the project to a scratch folder (so Desktop's caches never land in your repo and
parameters can point at local data), opens it, waits for Desktop's Analysis Services engine,
refreshes the model and runs every slice's query through ADOMD.NET.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from dax_parity import tmdl

WORKSPACE_ROOTS = [
    Path.home() / "Microsoft" / "Power BI Desktop Store App" / "AnalysisServicesWorkspaces",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces",
]


class DesktopError(RuntimeError):
    pass


def runner() -> list[str]:
    """Build the bundled .NET runner once per version into a cache folder, return the command to run it."""
    from dax_parity import __version__

    if not shutil.which("dotnet"):
        raise DesktopError("the .NET SDK is needed to talk to Power BI's engine: https://dotnet.microsoft.com/download")
    cache = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "dax-parity" / f"runner-{__version__}"
    dll = cache / "bin" / "DaxRunner.dll"
    if not dll.exists():
        src = cache / "src"
        src.mkdir(parents=True, exist_ok=True)
        for name in ("DaxRunner.csproj", "Program.cs"):
            (src / name).write_bytes(resources.files("dax_parity.runner").joinpath(name).read_bytes())
        print("building the .NET runner (first use only)...", file=sys.stderr)
        done = subprocess.run(["dotnet", "build", str(src), "-c", "Release", "-o", str(cache / "bin"), "-nologo", "-v", "q"],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise DesktopError("building the runner failed:\n" + done.stdout[-2000:] + done.stderr[-2000:])
    return ["dotnet", str(dll)]


def run(*args: str, check: bool = True, progress: bool = False) -> str:
    """Run the .NET runner. With progress, its stderr (one line per step) goes straight to the terminal."""
    done = subprocess.run([*runner(), *args], stdout=subprocess.PIPE, stderr=None if progress else subprocess.PIPE,
                          text=True)
    if check and done.returncode != 0:
        detail = (done.stderr or "").strip() or done.stdout.strip()
        raise DesktopError(f"DaxRunner {args[0]} failed:\n{detail}")
    return done.stdout


def prepare(pbip: Path, parameters: dict[str, str], into: Path) -> Path:
    """Copy the .pbip, its report and its semantic model, then set the parameters on the copy."""
    project = tmdl.locate(pbip)
    base = Path(os.path.commonpath([project.pbip.parent, project.report, project.semantic_model]))
    for folder in (project.report, project.semantic_model):
        shutil.copytree(folder, into / folder.relative_to(base),
                        ignore=shutil.ignore_patterns(".pbi", "cache.abf", "localSettings.json"))
    shutil.copy2(project.pbip, into / project.pbip.relative_to(base))
    copy = tmdl.locate(into / project.pbip.relative_to(base))
    if parameters:
        tmdl.set_parameters(copy.definition, parameters)
    return copy.pbip


def _newest_port(since: float) -> int | None:
    files = [f for root in WORKSPACE_ROOTS if root.exists() for f in root.rglob("msmdsrv.port.txt")]
    files = [f for f in files if f.stat().st_mtime > since]
    if not files:
        return None
    text = max(files, key=lambda f: f.stat().st_mtime).read_bytes().decode("utf-16-le", "ignore").strip("\x00 \r\n")
    return int(text) if text.isdigit() else None


def open_and_wait(pbip: Path, timeout: float) -> int:
    if sys.platform != "win32":
        raise DesktopError("recording needs Power BI Desktop, which only runs on Windows; on other systems use `check`")
    opened = time.time()
    os.startfile(pbip)  # noqa: S606 - opens with Power BI Desktop, the registered handler for .pbip
    while time.time() - opened < timeout:
        time.sleep(2)
        port = _newest_port(opened)
        if port and run("ping", str(port), check=False).strip() not in ("", "0"):
            return port
    raise DesktopError(f"Power BI Desktop did not load {pbip.name} within {timeout:.0f} s")


def close(title: str) -> None:
    """Close only the Desktop window this run opened, never other reports the user has open."""
    ps = (f"Get-Process PBIDesktop -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.MainWindowTitle -eq '{title.replace(chr(39), chr(39) * 2)}' }} | Stop-Process -Force")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


@contextmanager
def engine(pbip: Path, parameters: dict[str, str], *, port: int | None = None, refresh: bool = True,
           keep_open: bool = False, timeout: float = 300) -> Iterator[tuple[int, Path]]:
    """A Power BI Desktop engine with the model loaded and refreshed, plus a scratch folder."""
    with tempfile.TemporaryDirectory(prefix="dax-parity-") as tmp:
        tmp = Path(tmp)
        opened_here = port is None
        if opened_here:
            copy = prepare(pbip, parameters, tmp / "project")
            port = open_and_wait(copy, timeout)
            print(f"Power BI Desktop engine on port {port}", file=sys.stderr)
        try:
            if refresh:
                print(run("refresh", str(port)).strip(), file=sys.stderr)
            yield port, tmp
        finally:
            if opened_here and not keep_open:
                close(pbip.stem)
                time.sleep(2)  # let Desktop release the scratch folder before it is deleted


def record(pbip: Path, parameters: dict[str, str], queries: list[dict], **engine_args) -> dict:
    with engine(pbip, parameters, **engine_args) as (port, tmp):
        qfile, rfile = tmp / "queries.json", tmp / "results.json"
        qfile.write_text(json.dumps(queries), encoding="utf-8")
        print(run("query", str(port), str(qfile), str(rfile)).strip(), file=sys.stderr)
        return json.loads(rfile.read_text(encoding="utf-8"))


def mutate(pbip: Path, parameters: dict[str, str], jobs: list[dict], **engine_args) -> list[dict]:
    """Run each job (a measure, a changed expression, the queries that can see it) and return the rows."""
    with engine(pbip, parameters, **engine_args) as (port, tmp):
        jfile, rfile = tmp / "mutants.json", tmp / "mutants.out.json"
        jfile.write_text(json.dumps(jobs), encoding="utf-8")
        run("mutate", str(port), str(jfile), str(rfile), progress=True)
        return json.loads(rfile.read_text(encoding="utf-8"))
