from unittest.mock import patch

from source_scout import pattern_extractor
from source_scout.framework_detector import collect_all_files, detect_framework, read_file
from source_scout.language_patterns import identify_language_patterns
from source_scout.models import DeepPatternReport


def test_detect_framework_fastapi(tmp_path):
    d = tmp_path / "fastapi_project"
    d.mkdir()
    (d / "pyproject.toml").write_text("[project]")
    (d / "main.py").write_text("from fastapi import FastAPI")
    (d / "app").mkdir()
    (d / "app" / "main.py").write_text("app = FastAPI()")

    result = detect_framework(str(d))
    assert result == "fastapi"


def test_detect_framework_react(tmp_path):
    d = tmp_path / "react_project"
    d.mkdir()
    (d / "package.json").write_text('{"name": "my-app"}')
    (d / "src").mkdir()
    (d / "src" / "App.tsx").write_text("export default function App()")

    result = detect_framework(str(d))
    assert result == "react"


def test_detect_framework_nextjs(tmp_path):
    d = tmp_path / "nextjs_project"
    d.mkdir()
    (d / "next.config.js").write_text("module.exports = {}")
    (d / "package.json").write_text('{"name": "my-app"}')
    (d / "app").mkdir()
    (d / "app" / "layout.tsx").write_text("export default function Layout()")

    result = detect_framework(str(d))
    assert result == "next.js"


def test_detect_framework_none(tmp_path):
    d = tmp_path / "unknown_project"
    d.mkdir()
    (d / "readme.md").write_text("# My Project")

    result = detect_framework(str(d))
    assert result is None


def test_go_framework_ignored_if_python_present(tmp_path):
    d = tmp_path / "mixed_project"
    d.mkdir()
    (d / "go.mod").write_text("module example")
    (d / "pyproject.toml").write_text("[project]")

    result = detect_framework(str(d))
    assert result != "go"


def test_identify_language_patterns_fastapi(tmp_path):
    d = tmp_path / "fastapi_deep"
    d.mkdir()
    (d / "routers").mkdir()
    (d / "routers" / "users.py").write_text(
        "from fastapi import APIRouter, Depends\nrouter = APIRouter()\n"
        "def get_db():\n    return 'db'\n"
        "@router.get('/')\n"
        "async def list_users(db=Depends(get_db)):\n    return []"
    )
    (d / "routers" / "items.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/')\n"
        "async def list_items():\n    return []"
    )

    patterns = identify_language_patterns(str(d), "fastapi")
    assert len(patterns) >= 1
    assert any("router" in p.title.lower() for p in patterns)


def test_identify_language_patterns_react(tmp_path):
    d = tmp_path / "react_deep"
    d.mkdir()
    (d / "src").mkdir()
    (d / "src" / "App.tsx").write_text(
        "import { useState } from 'react'\n"
        "export default function App() {\n"
        "  const [count, setCount] = useState(0)\n"
        "  return <div>{count}</div>\n"
        "}"
    )
    (d / "src" / "Header.tsx").write_text(
        "export function Header() {\n  return <h1>Hello</h1>\n}"
    )

    patterns = identify_language_patterns(str(d), "react")
    assert len(patterns) >= 1
    assert any("component" in p.title.lower() for p in patterns)


def test_identify_language_patterns_no_framework(tmp_path):
    d = tmp_path / "no_fw"
    d.mkdir()
    patterns = identify_language_patterns(str(d), None)
    assert patterns == []


def test_collect_all_files(tmp_path):
    d = tmp_path / "collect_test"
    d.mkdir()
    (d / "README.md").write_text("# readme")
    (d / "src").mkdir()
    (d / "src" / "main.py").write_text("print('hi')")
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("config")

    files = collect_all_files(str(d))
    assert "README.md" in files
    assert "src/main.py" in files or any("main.py" in f for f in files)
    assert not any(".git" in f for f in files)


def test_read_file(tmp_path):
    f = tmp_path / "test_file.py"
    f.write_text("line1\nline2\nline3\n")
    content = read_file(str(f), max_lines=100)
    assert content == "line1\nline2\nline3"


def test_read_file_max_lines(tmp_path):
    f = tmp_path / "large_file.py"
    f.write_text("\n".join(f"line_{i}" for i in range(200)))
    content = read_file(str(f), max_lines=5)
    assert content is not None
    assert content.count("\n") == 4


def test_read_file_nonexistent():
    content = read_file("/nonexistent/path/file.py")
    assert content is None


def test_read_file_binary(tmp_path):
    f = tmp_path / "binary.bin"
    f.write_bytes(b"\x00\x01\x02\x03")
    content = read_file(str(f))
    assert isinstance(content, str)


def test_pattern_cache_save_and_lookup():
    with patch("source_scout.pattern_extractor._PATTERN_CACHE", {}):
        report = DeepPatternReport(
            owner="test",
            repo="repo",
            framework="fastapi",
        )
        pattern_extractor._PATTERN_CACHE["test/repo"] = report
        result = pattern_extractor._PATTERN_CACHE.get("test/repo")
        assert result is not None
        assert result.framework == "fastapi"


def test_pattern_cache_lookup_miss():
    with patch("source_scout.pattern_extractor._PATTERN_CACHE", {}):
        result = pattern_extractor._PATTERN_CACHE.get("nonexistent/repo")
        assert result is None
