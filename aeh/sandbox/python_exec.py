"""Execution backends for the `run_python` tool.

The agent's code runs in a throwaway working directory that contains a copy of the virtual filesystem
(`/data/x.csv` -> `./data/x.csv`). Files the code creates or modifies are synced back into the world.

Backends:
  * ``subprocess`` (default): a separate `python -I` process with a stripped environment and a timeout.
    Process isolation only; it is NOT a security boundary. Use it for trusted models in CI.
  * ``docker``: `docker run --network none` with memory/CPU/pid limits on `python:3.12-slim`.
    Use this when evaluating untrusted agents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_S = 15
MAX_OUTPUT_CHARS = 6000
DOCKER_IMAGE = os.environ.get("AEH_DOCKER_IMAGE", "python:3.12-slim")


def _materialize(files: dict[str, str], root: Path) -> None:
    for vpath, content in files.items():
        target = root / vpath.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")


def _collect(root: Path, skip: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = "/" + path.relative_to(root).as_posix()
        if rel in skip:
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable outputs are not synced back
    return out


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def run_python(code: str, files: dict[str, str], backend: str = "subprocess") -> tuple[str, dict[str, str]]:
    """Run `code`; return (combined output, updated file map)."""
    tmp = Path(tempfile.mkdtemp(prefix="aeh_py_"))
    work = tmp / "work"
    work.mkdir()
    try:
        _materialize(files, work)
        script_name = "_aeh_main.py"
        (work / script_name).write_text(code, encoding="utf-8")

        if backend == "docker":
            cmd = [
                "docker", "run", "--rm", "--network", "none", "--memory", "256m", "--cpus", "1",
                "--pids-limit", "64", "-v", f"{work.resolve()}:/work", "-w", "/work",
                DOCKER_IMAGE, "python", "-I", script_name,
            ]
            env = None
            timeout = TIMEOUT_S + 20  # container start-up overhead
        else:
            cmd = [sys.executable, "-I", script_name]
            keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG")
            env = {k: v for k, v in os.environ.items() if k.upper() in keep}
            env["PYTHONIOENCODING"] = "utf-8"
            timeout = TIMEOUT_S

        try:
            proc = subprocess.run(
                cmd, cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: execution timed out after {TIMEOUT_S}s", files

        out = proc.stdout
        if proc.stderr:
            out += ("\n" if out else "") + "[stderr]\n" + proc.stderr
        if proc.returncode != 0:
            out += f"\n[exit code {proc.returncode}]"
        new_files = _collect(work, skip={"/" + script_name})
        return _truncate(out.strip() or "(no output)"), new_files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
