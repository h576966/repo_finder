import os

from .framework_detector import read_file
from .models import Pattern


def identify_language_patterns(repo_path: str, framework: str | None) -> list[Pattern]:
    patterns: list[Pattern] = []
    if not framework:
        return patterns

    router_count = 0
    dep_count = 0
    component_count = 0
    hook_count = 0

    for dirpath, _dirnames, filenames in os.walk(repo_path):
        for f in filenames:
            filepath = os.path.join(dirpath, f)
            content: str | None = None

            if framework == "fastapi" and f.endswith(".py"):
                content = read_file(filepath, max_lines=100)
                if content:
                    router_count += content.count("APIRouter")
                    dep_count += content.count("Depends(")

            elif framework in ("react", "next.js") and f.endswith((".tsx", ".jsx")):
                content = read_file(filepath, max_lines=100)
                if content:
                    if "export default function" in content or "export function" in content:
                        component_count += 1
                    if "useState" in content or "useEffect" in content:
                        hook_count += 1

    if framework == "fastapi":
        if router_count > 0:
            patterns.append(Pattern(
                category="code_structure",
                title="FastAPI router pattern",
                description=f"Found {router_count} APIRouter usage(s) — modular route organization",
                snippet=f"APIRouter count: {router_count}, Depends count: {dep_count}",
                source="file_preview",
            ))
        if "alembic" in " ".join(os.listdir(repo_path)):
            patterns.append(Pattern(
                category="best_practice",
                title="Database migrations (Alembic)",
                description="Project uses Alembic for database migrations",
                snippet=None,
                source="file_tree",
            ))

    if framework in ("react", "next.js") and component_count > 0:
        patterns.append(Pattern(
            category="code_structure",
            title="React component structure",
            description=f"Found {component_count} React component(s) with {hook_count} hook usage(s)",
            snippet=f"Components: {component_count}, Hooks: {hook_count}",
            source="file_preview",
        ))

    if framework in ("django", "flask"):
        patterns.append(Pattern(
            category="code_structure",
            title=f"{framework.title()} web application structure detected",
            description=f"Standard {framework} project layout identified",
            snippet=None,
            source="file_tree",
        ))

    return patterns
