from __future__ import annotations

import subprocess
import sys


def _check_commands(with_local_explore_eval: bool) -> list[list[str]]:
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
        [sys.executable, "-m", "pytest", "-q"],
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
                "6",
                "--label",
                "check-local-explore",
            ]
        )
    return commands


def _run_check_commands(with_local_explore_eval: bool) -> None:
    for command in _check_commands(with_local_explore_eval):
        print(f"\n==> {subprocess.list2cmdline(command)}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            sys.exit(completed.returncode)
    print("\nAll checks passed.", flush=True)
