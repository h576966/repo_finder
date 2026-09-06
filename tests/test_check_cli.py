from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import source_scout.__main__ as main_module
from source_scout import cli_checks


@pytest.fixture
def check_workspace(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_checks, "_checkout_identity", lambda cwd: {"content_sha256": "abc", "error": None}
    )
    return tmp_path


def _fake_checks(monkeypatch, *, codes=None, outputs=None, states=None):
    calls = []
    codes, outputs, states = codes or {}, outputs or {}, states or {}

    def execute(argv, cwd, stdout, stderr, timeout):
        name = argv[2]
        calls.append(argv)
        stdout.write_text(outputs.get(name, "[]" if name == "ruff" else "OK"), encoding="utf-8")
        stderr.write_text(outputs.get(name + "_stderr", ""), encoding="utf-8")
        if name == "pytest":
            (stdout.parent / "pytest.xml").write_text(
                outputs.get("xml", '<testsuites><testsuite><testcase name="one"/></testsuite></testsuites>'),
                encoding="utf-8",
            )
        code = codes.get(name, 0)
        status = states.get(name, "failed" if code else "passed")
        return status, code, "Interrupted" if status == "cancelled" else None

    monkeypatch.setattr(cli_checks, "_execute", execute)
    return calls


def _run(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["source-scout", "check", "--format", "json", *args])
    code = 0
    try:
        main_module.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert "Checking" not in captured.out
    return json.loads(captured.out), code


def test_check_cli_runs_default_commands(check_workspace, monkeypatch, capsys):
    calls = _fake_checks(monkeypatch)
    report, code = _run(monkeypatch, capsys)
    assert code == 0 and report["success"]
    assert [c[2] for c in calls] == ["ruff", "mypy", "pytest"]
    assert calls[0][-2:] == ["--output-format", "json"]
    assert report["schema_version"] == cli_checks.SCHEMA_VERSION
    assert report["checkout_before"] == report["checkout_after"]
    saved = Path(report["report_path"])
    assert json.loads(saved.read_text()) == report
    assert (saved.parent.parent / ".gitignore").read_text() == "/*\n"
    for check in report["checks"]:
        assert Path(check["stdout_path"]).exists()
        assert Path(check["stderr_path"]).exists()
        assert check["cwd"] == str(check_workspace)
        assert check["exit_code"] == 0
        assert check["duration_seconds"] >= 0
    assert report["checks"][2]["tests"]["collected"] == 1


def test_check_cli_local_explore_flag_appends_eval(check_workspace, monkeypatch, capsys):
    calls = _fake_checks(monkeypatch)
    report, code = _run(monkeypatch, capsys, "--with-local-explore-eval")
    assert calls[-1][2:4] == ["source_scout", "eval-local-explore"]
    assert len(report["checks"]) == 4 and code == 0


def test_check_cli_runs_remaining_checks_after_failure(check_workspace, monkeypatch, capsys):
    calls = _fake_checks(monkeypatch, codes={"ruff": 1, "mypy": 1})
    report, code = _run(monkeypatch, capsys)
    assert len(calls) == 3 and code == 1
    assert [c["status"] for c in report["checks"]] == ["failed", "failed", "passed"]
    assert not report["success"]


def test_check_commands_use_unique_workspace_roots(check_workspace):
    first = cli_checks._check_commands(False)[2]
    second = cli_checks._check_commands(False)[2]
    assert first[4] == "--basetemp"
    assert Path(first[5]).parent != Path(second[5]).parent
    assert Path(first[5]).parent.parent == check_workspace / ".source_scout" / "checks"


@pytest.mark.parametrize(
    ("outputs", "codes", "name", "parse_status"),
    [
        ({"ruff": "not json"}, {}, "ruff", "error"),
        ({"xml": "<bad"}, {}, "pytest", "error"),
        ({"xml": "<testsuites/>"}, {"pytest": 5}, "pytest", "error"),
        ({"xml": "<testsuites/>"}, {}, "pytest", "error"),
        ({"mypy_stderr": "No module named mypy"}, {"mypy": 1}, "mypy", "error"),
        (
            {"xml": '<testsuites><testcase><error message="collection failed"/></testcase></testsuites>'},
            {"pytest": 2},
            "pytest",
            "parsed",
        ),
    ],
)
def test_invalid_results_never_pass(check_workspace, monkeypatch, capsys, outputs, codes, name, parse_status):
    _fake_checks(monkeypatch, codes=codes, outputs=outputs)
    report, code = _run(monkeypatch, capsys)
    check = next(c for c in report["checks"] if c["name"] == name)
    assert code == 1 and check["status"] == "error"
    assert check["parse_status"] == parse_status
    assert check["errors"]


def test_truncation_keeps_original_logs(check_workspace, monkeypatch, capsys):
    output = '[{"long": "' + "x" * 500 + '"}]'
    monkeypatch.setattr(cli_checks, "MAX_PARSE_BYTES", 200)
    _fake_checks(monkeypatch, outputs={"ruff": output})
    report, code = _run(monkeypatch, capsys)
    check = report["checks"][0]
    assert code == 1 and check["output_truncated"] and check["parse_status"] == "error"
    assert Path(check["stdout_path"]).read_text() == output


def test_windows_diagnostic_paths_and_summary_cap(check_workspace, monkeypatch, capsys):
    diagnostic = {
        "filename": r"C:\my project\src\file.py",
        "location": {"row": 2, "column": 4},
        "code": "F821",
        "message": "Unknown symbol",
    }
    _fake_checks(monkeypatch, codes={"ruff": 1}, outputs={"ruff": json.dumps([diagnostic] * 15)})
    report, code = _run(monkeypatch, capsys)
    check = report["checks"][0]
    assert code == 1 and check["errors_truncated"]
    assert len(check["errors"]) == cli_checks.MAX_ERRORS
    assert check["errors"][0].startswith(r"C:\my project\src\file.py:2:4:")


def test_skip_and_xfail_are_separate(check_workspace, monkeypatch, capsys):
    _fake_checks(
        monkeypatch,
        outputs={
            "xml": "<testsuites><testsuite>"
            '<testcase><skipped type="pytest.skip"/></testcase>'
            '<testcase><skipped type="pytest.xfail"/></testcase></testsuite></testsuites>'
        },
    )
    report, code = _run(monkeypatch, capsys)
    assert code == 0
    assert report["checks"][2]["tests"] == {
        "collected": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "xfailed": 1,
        "xpassed": None,
    }


@pytest.mark.parametrize(
    ("state", "expected", "exit_code"),
    [
        ("timeout", ["timeout", "passed", "passed"], 1),
        ("cancelled", ["cancelled", "not_run", "not_run"], 130),
        ("error", ["error", "passed", "passed"], 1),
    ],
)
def test_timeout_cancellation_missing_tool_reports(
    check_workspace, monkeypatch, capsys, state, expected, exit_code
):
    _fake_checks(monkeypatch, states={"ruff": state})
    report, code = _run(monkeypatch, capsys)
    assert code == exit_code
    assert [c["status"] for c in report["checks"]] == expected


def test_changed_checkout_is_not_verification(check_workspace, monkeypatch, capsys):
    _fake_checks(monkeypatch)
    identities = iter([{"content_sha256": "old", "error": None}, {"content_sha256": "new", "error": None}])
    monkeypatch.setattr(cli_checks, "_checkout_identity", lambda cwd: next(identities))
    report, code = _run(monkeypatch, capsys)
    assert code == 1 and report["checks_passed"] and report["changes_detected"]
    assert not report["success"]


def test_process_missing_executable(tmp_path):
    status, code, error = cli_checks._execute(
        [str(tmp_path / "does not exist.exe")],
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
        1,
    )
    assert status == "error" and code is None and error


def test_process_timeout_reaps_child(tmp_path):
    status, code, error = cli_checks._execute(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
        0.1,
    )
    assert status == "timeout" and code is not None and error


def test_cancellation_terminates_process_tree(monkeypatch, tmp_path):
    from io import BytesIO

    class Job:
        def assign(self, pid):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli_checks, "WindowsCheckJob", Job)

    class Process:
        pid = 123
        stdin = BytesIO()
        returncode = None

        def wait(self, timeout):
            raise KeyboardInterrupt

    process = Process()
    reaped = []
    monkeypatch.setattr(cli_checks.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(cli_checks, "_terminate_tree", lambda p, job=None: reaped.append(p))
    status, code, error = cli_checks._execute(["python"], tmp_path, tmp_path / "out", tmp_path / "err", 1)
    assert status == "cancelled" and reaped == [process]


def test_project_python_handles_spaces(monkeypatch, tmp_path):
    root = tmp_path / "my project"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.chdir(root)
    assert cli_checks._check_commands(False)[0][0] == str(python)


def test_checkout_identity_detects_dirty_content_and_ignores_generated(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    (tmp_path / ".gitignore").write_text(".source_scout/\n")
    source = tmp_path / "file.py"
    source.write_text("a = 1\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    initial = cli_checks._checkout_identity(tmp_path)
    assert initial["error"] is None
    source.write_text("a = 2\n")
    changed = cli_checks._checkout_identity(tmp_path)
    assert changed["content_sha256"] != initial["content_sha256"]
    source.write_text("a = 3\n")
    assert cli_checks._checkout_identity(tmp_path)["content_sha256"] != changed["content_sha256"]
    before_generated = cli_checks._checkout_identity(tmp_path)
    (tmp_path / ".source_scout").mkdir()
    (tmp_path / ".source_scout" / "log").write_text("generated")
    assert cli_checks._checkout_identity(tmp_path) == before_generated


def test_normal_cli_import_does_not_load_catalog():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import source_scout.__main__; "
            'assert "source_scout.catalog" not in sys.modules; assert "duckdb" not in sys.modules',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_interrupted_parser_keeps_report_and_marks_not_run(check_workspace, monkeypatch, capsys):
    _fake_checks(monkeypatch)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_checks, "_parse_result", interrupt)
    report, code = _run(monkeypatch, capsys)
    assert code == 130
    assert [c["status"] for c in report["checks"]] == ["cancelled", "not_run", "not_run"]


def test_downloaded_checkout_is_rejected_before_execution(monkeypatch, tmp_path):
    root = tmp_path / ".source_scout" / "repos" / "downloaded"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    with pytest.raises(ValueError, match="downloaded repositories"):
        cli_checks._run_check_commands(False)


def test_unknown_checkout_identity_never_verifies(check_workspace, monkeypatch, capsys):
    _fake_checks(monkeypatch)
    monkeypatch.setattr(cli_checks, "_checkout_identity", lambda cwd: {"error": "git unavailable"})
    report, code = _run(monkeypatch, capsys)
    assert code == 1 and report["changes_detected"] is None and not report["success"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree regression")
def test_windows_timeout_terminates_descendants(tmp_path):
    import ctypes
    from ctypes import wintypes

    pid_path = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; from pathlib import Path; "
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    status, code, error = cli_checks._execute(
        [sys.executable, "-c", script, str(pid_path)],
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
        1.0,
    )
    assert status == "timeout" and pid_path.exists()
    child_pid = int(pid_path.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, child_pid)
    if not handle:
        assert ctypes.get_last_error() == 87  # PID already removed.
        return
    try:
        wait_result = kernel.WaitForSingleObject(handle, 1000)
        if wait_result == 258:
            subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"], capture_output=True, check=False)
        assert wait_result == 0, "Timeout left a descendant process running"
    finally:
        kernel.CloseHandle(handle)
