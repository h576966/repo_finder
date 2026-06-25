from __future__ import annotations

import subprocess
import sys

import pytest

import source_scout.__main__ as main_module
from source_scout import cli_checks, fastcontext


def _completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[object]:
    return subprocess.CompletedProcess(command, returncode)


def test_check_cli_runs_default_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[object]:
        assert check is False
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(cli_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["source-scout", "check"])

    main_module.main()

    assert calls == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
        [sys.executable, "-m", "pytest", "-q"],
    ]
    assert "All checks passed." in capsys.readouterr().out


def test_check_cli_local_explore_flag_appends_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[object]:
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(cli_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "check",
            "--with-local-explore-eval",
        ],
    )

    main_module.main()

    assert calls[-1] == [
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
    assert calls == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
        [sys.executable, "-m", "pytest", "-q"],
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
        ],
    ]


def test_check_cli_exits_on_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[object]:
        calls.append(command)
        return _completed(command, returncode=7 if len(calls) == 2 else 0)

    monkeypatch.setattr(cli_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["source-scout", "check"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 7
    assert calls == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
    ]
