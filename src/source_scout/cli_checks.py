from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .check_process import WINDOWS_CHECK_LAUNCHER, WindowsCheckJob

SCHEMA_VERSION = "source-scout-check-v1"
MAX_PARSE_BYTES = 4 * 1024 * 1024
MAX_ERRORS = 10


def _check_run_root() -> Path:
    return Path.cwd() / ".source_scout" / "checks" / uuid.uuid4().hex


def _project_python(cwd: Path) -> str:
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = cwd / relative
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _check_commands(with_local_explore_eval: bool, run_root: Path | None = None) -> list[list[str]]:
    active_root = run_root or _check_run_root()
    python = _project_python(Path.cwd())
    commands = [
        [python, "-m", "ruff", "check", ".", "--output-format", "json"],
        [python, "-m", "mypy", "src"],
        [
            python,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(active_root / "pytest-temp"),
            "-o",
            f"cache_dir={active_root / 'pytest-cache'}",
            f"--junitxml={active_root / 'pytest.xml'}",
        ],
    ]
    if with_local_explore_eval:
        from .fastcontext_constants import DEFAULT_MAX_TURNS

        commands.append(
            [
                python,
                "-m",
                "source_scout",
                "eval-local-explore",
                "--suite",
                "source-scout",
                "--max-turns",
                str(DEFAULT_MAX_TURNS),
                "--label",
                "check-local-explore",
            ]
        )
    return commands


def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout


def _checkout_identity(cwd: Path) -> dict[str, Any]:
    """Identify tracked and non-ignored untracked contents before/after a run.

    Ignored files and transient edits reverted between observations are outside
    this identity. It is not an atomic filesystem snapshot.
    """
    try:
        root = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel")).strip()).resolve()
        paths = sorted(
            set(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
        )
        digest = hashlib.sha256()
        for raw in paths:
            if not raw:
                continue
            path = root / os.fsdecode(raw)
            digest.update(raw + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(65536), b""):
                        digest.update(chunk)
            else:
                digest.update(b"missing\0")
            digest.update(b"\0")
        return {
            "root": str(root),
            "head": _git(root, "rev-parse", "HEAD").decode().strip(),
            "status_sha256": hashlib.sha256(_git(root, "status", "--porcelain=v1", "-z")).hexdigest(),
            "content_sha256": digest.hexdigest(),
            "error": None,
            "scope": "tracked and non-ignored untracked files; before/after observation",
        }
    except KeyboardInterrupt:
        return {
            "root": str(cwd),
            "head": None,
            "content_sha256": None,
            "error": "Interrupted while identifying working copy.",
            "cancelled": True,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"root": str(cwd), "head": None, "content_sha256": None, "error": str(exc)}


def _terminate_tree(process: subprocess.Popen[bytes], job: WindowsCheckJob | None = None) -> None:
    if job is not None:
        job.terminate()
    elif os.name != "nt":
        try:
            getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def _execute(
    argv: list[str], cwd: Path, stdout: Path, stderr: Path, timeout: float
) -> tuple[str, int | None, str | None]:
    process = None
    job = None
    started = time.monotonic()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            command = argv
            if os.name == "nt":
                job = WindowsCheckJob()
                command = [argv[0], "-c", WINDOWS_CHECK_LAUNCHER, *argv]
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=out,
                stderr=err,
                stdin=subprocess.PIPE if job else subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            if job is not None:
                job.assign(process.pid)
                assert process.stdin is not None
                process.stdin.write(b"1")
                process.stdin.close()
            code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            return ("passed" if code == 0 else "failed"), code, None
        except subprocess.TimeoutExpired:
            if process is not None:
                _terminate_tree(process, job)
            return "timeout", process.returncode if process else None, "Check exceeded its deadline."
        except KeyboardInterrupt:
            if process is not None:
                _terminate_tree(process, job)
            return "cancelled", process.returncode if process else None, "Interrupted by user."
        except OSError as exc:
            if process is not None:
                _terminate_tree(process, job)
            return "error", None, str(exc)
        finally:
            if job is not None:
                job.close()


def _read_output(path: Path) -> tuple[str, bool]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_PARSE_BYTES + 1)
    return raw[:MAX_PARSE_BYTES].decode("utf-8", errors="replace"), len(raw) > MAX_PARSE_BYTES


def _parse_result(name: str, item: dict[str, Any], run_root: Path) -> None:
    stdout, out_cut = _read_output(Path(item["stdout_path"]))
    stderr, err_cut = _read_output(Path(item["stderr_path"]))
    item["output_truncated"] = out_cut or err_cut
    errors: list[str] = []
    try:
        if "No module named" in stderr:
            raise ValueError("Required Python module is unavailable.")
        if name == "ruff":
            if out_cut:
                raise ValueError("Ruff JSON exceeds parser byte limit; see original stdout.")
            diagnostics = json.loads(stdout)
            if not isinstance(diagnostics, list):
                raise ValueError("Expected Ruff JSON diagnostic array.")
            for diagnostic in diagnostics:
                location = diagnostic["location"]
                errors.append(
                    f"{diagnostic['filename']}:{location['row']}:{location['column']}: "
                    f"{diagnostic['code']}: {diagnostic['message']}"
                )
            if errors and item["status"] == "passed":
                item["status"] = "failed"
        elif name == "pytest":
            xml_path = run_root / "pytest.xml"
            if xml_path.stat().st_size > MAX_PARSE_BYTES:
                item["output_truncated"] = True
                raise ValueError("JUnit XML exceeds parser byte limit; see original XML.")
            root = ET.parse(xml_path).getroot()
            cases = root.findall(".//testcase")
            failures = root.findall(".//failure")
            collection_errors = root.findall(".//error")
            skipped = root.findall(".//skipped")
            xfailed = sum(node.get("type") == "pytest.xfail" for node in skipped)
            item["tests"] = {
                "collected": len(cases),
                "failed": len(failures),
                "errors": len(collection_errors),
                "skipped": len(skipped) - xfailed,
                "xfailed": xfailed,
                "xpassed": None,
            }
            errors.extend(
                node.get("message") or node.text or node.tag for node in [*failures, *collection_errors]
            )
            if not cases:
                raise ValueError("No tests collected.")
            if collection_errors or item["exit_code"] in (2, 3, 4, 5):
                item["status"] = "error"
            elif failures and item["status"] == "passed":
                item["status"] = "failed"
        elif item["status"] != "passed":
            errors = [line for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        item["parse_status"] = "text" if name not in {"ruff", "pytest"} else "parsed"
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
        item["parse_status"] = "error"
        item["status"] = "error"
        errors.append(f"Result parsing failed: {exc}")
    if item["status"] != "passed" and not errors:
        errors = [line for line in stderr.splitlines() if line.strip()] or [
            f"Command exited with code {item['exit_code']}; see original logs."
        ]
    item["errors_truncated"] = len(errors) > MAX_ERRORS or any(len(e) > 500 for e in errors)
    item["errors"] = [e[:500] for e in errors[:MAX_ERRORS]]


def _run_check_commands(
    with_local_explore_eval: bool, *, output_format: str = "text", timeout_seconds: float = 300.0
) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Check timeout must be a positive finite number.")
    cwd = Path.cwd().resolve()
    storage = Path(os.environ.get("SOURCE_SCOUT_HOME", str(cwd / ".source_scout"))).resolve()
    in_default_downloads = any(
        part.lower() == ".source_scout"
        and index + 1 < len(cwd.parts)
        and cwd.parts[index + 1].lower() == "repos"
        for index, part in enumerate(cwd.parts)
    )
    if in_default_downloads or cwd.is_relative_to(storage / "repos"):
        raise ValueError("Run checks from the trusted working copy, never downloaded repositories.")
    run_root = _check_run_root()
    run_root.mkdir(parents=True, exist_ok=False)
    ignore = run_root.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("/*\n", encoding="utf-8")
    before = _checkout_identity(cwd)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_root.name,
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": str(cwd),
        "checkout_before": before,
        "checks": [],
        "report_path": str(run_root / "report.json"),
    }
    cancelled = bool(before.get("cancelled"))
    commands = _check_commands(with_local_explore_eval, run_root)
    for name, argv in zip(("ruff", "mypy", "pytest", "local-explore-eval"), commands, strict=False):
        item: dict[str, Any] = {
            "name": name,
            "argv": argv,
            "cwd": str(cwd),
            "required": True,
            "status": "not_run",
            "exit_code": None,
            "duration_seconds": 0.0,
            "stdout_path": str(run_root / f"{name}.stdout.log"),
            "stderr_path": str(run_root / f"{name}.stderr.log"),
            "errors": [],
            "parse_status": "not_run",
            "output_truncated": False,
            "errors_truncated": False,
        }
        report["checks"].append(item)
        if cancelled:
            item["errors"] = ["Not run because a preceding check was cancelled."]
            continue
        started = time.monotonic()
        try:
            print(f"Checking {name}...", file=sys.stderr, flush=True)
            status, code, error = _execute(
                argv, cwd, Path(item["stdout_path"]), Path(item["stderr_path"]), timeout_seconds
            )
            item.update(status=status, exit_code=code)
            if error:
                item["errors"] = [error]
            if status in {"passed", "failed"}:
                _parse_result(name, item, run_root)
            cancelled = status == "cancelled"
        except KeyboardInterrupt:
            item.update(status="cancelled", errors=["Interrupted while processing the check result."])
            cancelled = True
        except (OSError, subprocess.SubprocessError) as exc:
            item.update(status="error", errors=[str(exc)])
        finally:
            item["duration_seconds"] = round(time.monotonic() - started, 3)
    after = _checkout_identity(cwd)
    cancelled = cancelled or bool(after.get("cancelled"))
    report["checkout_after"] = after
    report["changes_detected"] = before != after if not before["error"] and not after["error"] else None
    report["checks_passed"] = all(item["status"] == "passed" for item in report["checks"])
    report["success"] = report["checks_passed"] and report["changes_detected"] is False
    report["finished_at"] = datetime.now(UTC).isoformat()
    rendered = json.dumps(report, indent=2, ensure_ascii=True)
    Path(report["report_path"]).write_text(rendered + "\n", encoding="utf-8")
    if output_format == "json":
        print(rendered)
    else:
        for item in report["checks"]:
            print(f"{item['name']}: {item['status']} ({item['duration_seconds']:.2f}s)")
            for error in item["errors"][:2]:
                print(f"  {error}")
        if report["changes_detected"] is not False:
            print("Working copy changed or could not be identified; this run does not verify current code.")
        print(f"Report: {report['report_path']}")
    if not report["success"]:
        raise SystemExit(130 if cancelled else 1)
