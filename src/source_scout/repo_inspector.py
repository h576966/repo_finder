from datetime import UTC, datetime
from typing import Any

import httpx

from .constants import _now_iso
from .github_client import get_client
from .models import InspectionResult, QualityReport, RepoStructure


def _evaluate_activity(pushed_at: str | None) -> str:
    if not pushed_at:
        return "unknown"
    try:
        pushed_dt = datetime.fromisoformat(pushed_at)
    except (ValueError, AttributeError):
        return "unknown"
    days = (datetime.now(UTC) - pushed_dt).days
    if days <= 90:
        return "active"
    if days <= 365:
        return "moderate"
    return "stale"


def _extract_key_files(structure: RepoStructure) -> list[str]:
    config_patterns = [
        "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "Cargo.toml", "go.mod",
        "Makefile", "Dockerfile", "docker-compose.yml",
        ".github/workflows", ".github/actions",
    ]
    entry_patterns = ["main.py", "app.py", "index.js", "index.ts", "main.go", "main.rs"]
    key_files: list[str] = []

    all_names = structure.files + structure.dirs
    for pattern in config_patterns:
        if pattern in all_names or pattern in structure.files:
            key_files.append(pattern)

    for name in all_names:
        if "readme" in name.lower() and name not in key_files:
            key_files.append(name)

    for pattern in entry_patterns:
        if pattern in all_names:
            if pattern not in key_files:
                key_files.append(pattern)

    return key_files


async def analyze_structure(owner: str, repo: str) -> RepoStructure:
    client = get_client()
    try:
        contents = await client.get_repo_contents(owner, repo, "")
    except httpx.HTTPStatusError:
        return RepoStructure()

    if isinstance(contents, dict):
        return RepoStructure()

    dirs: list[str] = []
    files: list[str] = []
    for entry in contents:
        if isinstance(entry, dict):
            etype = entry.get("type", "")
            name = entry.get("name", "")
            if etype == "dir":
                dirs.append(name)
            elif etype == "file":
                files.append(name)

    structure = RepoStructure(dirs=dirs, files=files)
    structure.key_files = _extract_key_files(structure)
    return structure


async def evaluate_quality(
    owner: str,
    repo: str,
    metadata: dict[str, Any],
    readme: str | None,
    structure: RepoStructure,
) -> QualityReport:
    signals: dict[str, str] = {}
    score = 0.0

    if readme:
        readme_len = len(readme)
        if readme_len > 2000:
            signals["readme"] = "comprehensive"
            score += 0.4
        elif readme_len > 500:
            signals["readme"] = "adequate"
            score += 0.3
        else:
            signals["readme"] = "minimal"
            score += 0.1
    else:
        signals["readme"] = "missing"

    lic = metadata.get("license")
    if lic:
        signals["license"] = lic.get("spdx_id", "present")
        score += 0.15
    else:
        signals["license"] = "missing"

    if metadata.get("description"):
        score += 0.1

    has_ci = any(
        ".github/workflows" in kf or ".github/actions" in kf
        for kf in structure.key_files
    )
    if has_ci:
        signals["ci"] = "present"
        score += 0.15
    else:
        signals["ci"] = "unknown"

    open_issues = metadata.get("open_issues_count", 0) or 0
    if open_issues == 0:
        signals["issues"] = "none"
        score += 0.1
    elif open_issues <= 50:
        signals["issues"] = "few"
        score += 0.05
    else:
        signals["issues"] = "many"

    pushed_at = metadata.get("pushed_at")
    activity = _evaluate_activity(pushed_at)
    signals["activity"] = activity
    if activity == "active":
        score += 0.1
    elif activity == "moderate":
        score += 0.05

    return QualityReport(signals=signals, score=round(min(score, 1.0), 4))


def _determine_verdict(metadata: dict[str, Any], quality: QualityReport) -> tuple[str, str]:
    # Verdict domain: full repo inspection — archive status + quality signals
    if metadata.get("archived", False):
        return "skip", "Repository is archived"
    if quality.score >= 0.7:
        return "useful", "High quality: well-documented and maintained"
    if quality.score >= 0.4:
        reasoning = "Moderate quality"
        if quality.signals.get("readme") == "missing":
            reasoning += " — missing README"
        if quality.signals.get("license") == "missing":
            reasoning += " — no license"
        return "maybe", reasoning
    return "skip", "Low quality signals"


async def inspect_repo(owner: str, repo: str) -> InspectionResult:
    client = get_client()

    metadata = await client.get_repo_metadata(owner, repo)
    readme = await client.get_readme(owner, repo)
    structure = await analyze_structure(owner, repo)
    quality = await evaluate_quality(owner, repo, metadata, readme, structure)

    verdict, reasoning = _determine_verdict(metadata, quality)

    readme_preview = None
    if readme:
        readme_preview = readme[:500]

    return InspectionResult(
        owner=owner,
        repo=repo,
        description=metadata.get("description"),
        language=metadata.get("language"),
        stars=metadata.get("stargazers_count", 0) or 0,
        forks=metadata.get("forks_count", 0) or 0,
        open_issues=metadata.get("open_issues_count", 0) or 0,
        license_name=(metadata.get("license") or {}).get("spdx_id"),
        last_push=metadata.get("pushed_at", ""),
        archived=metadata.get("archived", False),
        structure=structure,
        quality=quality,
        readme_preview=readme_preview,
        verdict=verdict,
        verdict_reasoning=reasoning,
        cached=False,
        timestamp=_now_iso(),
    )
