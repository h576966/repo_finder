import pytest

from source_scout.pattern_extractor import (
    _SECTION_RE,
    _matches_focus,
    distill_patterns,
    parse_readme_sections,
)

pytestmark = pytest.mark.legacy


def test_parse_readme_sections():
    readme = """# Title

Introduction text.

## Installation

Run `pip install`.

## Usage

```python
import foo
```

## Architecture

Project uses hexagonal architecture.
"""
    sections = parse_readme_sections(readme)
    assert "Installation" in sections
    assert "Usage" in sections
    assert "Architecture" in sections
    assert "pip install" in sections["Installation"]
    assert "hexagonal" in sections["Architecture"]


def test_parse_readme_sections_empty():
    assert parse_readme_sections("") == {}


def test_parse_readme_sections_no_headers():
    readme = "Just plain text without any markdown headers."
    assert parse_readme_sections(readme) == {}


def test_matches_focus_exact():
    assert _matches_focus("This is about auth and tokens", "auth") is True


def test_matches_focus_no_match():
    assert _matches_focus("This is about deployment", "auth") is False


def test_matches_focus_with_keywords():
    assert _matches_focus("Uses JWT for authentication", "auth") is True


def test_section_re_pattern():
    assert _SECTION_RE.match("## Title")
    assert _SECTION_RE.match("##  Title with spaces")
    assert not _SECTION_RE.match("# Title")
    assert not _SECTION_RE.match("### Subtitle")


def test_distill_patterns_architecture():
    sections = {"Architecture": "Hexagonal architecture with domain-driven design."}
    file_tree = ["src", "tests", "pyproject.toml"]
    previews = {}
    patterns = distill_patterns(sections, file_tree, previews, focus=None)
    assert len(patterns) >= 1
    assert any(p.category == "architecture" for p in patterns)


def test_distill_patterns_with_focus():
    sections = {"Architecture": "Uses JWT for auth", "Installation": "pip install"}
    file_tree = ["src", "tests"]
    previews = {}
    patterns = distill_patterns(sections, file_tree, previews, focus="auth")
    assert any(p.category == "architecture" for p in patterns)
    assert any("JWT" in p.description or "jwt" in p.description.lower() for p in patterns)
