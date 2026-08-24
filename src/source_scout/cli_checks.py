from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import fastcontext


def _check_run_root() -> Path:
    return Path.cwd() / ".source_scout" / "checks" / f"{os.getpid()}-{uuid.uuid4().hex}"


def _check_commands(with_local_explore_eval: bool, run_root: Path | None = None) -> list[list[str]]:
    active_root = run_root or _check_run_root()
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(active_root / "pytest-temp"),
            "-o",
            f"cache_dir={active_root / 'pytest-cache'}",
        ],
    ]
    if with_local_explore_eval:
        commands.append(
            [
                sys.executable,
                "-m",
                "source_scout",
                "eval-local-explore",
                "--suite",
                "source-scout",
                "--max-turns",
                str(fastcontext.DEFAULT_MAX_TURNS),
                "--label",
                "check-local-explore",
            ]
        )
    return commands


def _run_check_commands(with_local_explore_eval: bool) -> None:
    run_root = _check_run_root()
    run_root.mkdir(parents=True, exist_ok=False)
    try:
        for command in _check_commands(with_local_explore_eval, run_root):
            print(f"\n==> {subprocess.list2cmdline(command)}", flush=True)
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                sys.exit(completed.returncode)
        print("\nAll checks passed.", flush=True)
    finally:
        shutil.rmtree(run_root, ignore_errors=True)
