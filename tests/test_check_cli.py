from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import source_scout.__main__ as main_module
from source_scout import cli_checks, fastcontext


def _completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[object]:
    return subprocess.CompletedProcess(command, returncode)


def _assert_sandboxed_pytest_command(command: list[str]) -> Path:
    assert command[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert command[4] == "--basetemp"
    run_root = Path(command[5]).parent
    assert run_root.parent == Path.cwd() / ".source_scout" / "checks"
    assert command[6] == "-o"
    assert command[7] == f"cache_dir={run_root / 'pytest-cache'}"
    return run_root


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
    monkeypatch.setattr(sys, "argv", ["source_scout", "check"])

    main_module.main()

    assert calls[:2] == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
    ]
    run_root = _assert_sandboxed_pytest_command(calls[2])
    assert not run_root.exists()
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
            "source_scout",
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
    assert calls[:2] == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
    ]
    _assert_sandboxed_pytest_command(calls[2])


def test_check_cli_exits_on_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[object]:
        calls.append(command)
        return _completed(command, returncode=7 if len(calls) == 2 else 0)

    monkeypatch.setattr(cli_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["source_scout", "check"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 7
    assert calls == [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src"],
    ]


def test_check_commands_use_unique_workspace_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    first = cli_checks._check_commands(False)[2]
    second = cli_checks._check_commands(False)[2]

    first_root = _assert_sandboxed_pytest_command(first)
    second_root = _assert_sandboxed_pytest_command(second)
    assert first_root != second_root
