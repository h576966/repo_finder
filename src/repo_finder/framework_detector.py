import os

from .constants import SKIP_DIRS

_FRAMEWORK_MARKERS: dict[str, list[str]] = {
    "fastapi": ["setup.py", "pyproject.toml", "main.py", "app/main.py"],
    "flask": ["app.py", "wsgi.py", "requirements.txt"],
    "django": ["manage.py", "settings.py", "urls.py", "wsgi.py"],
    "react": ["package.json", "src/App.tsx", "src/App.jsx", "public/index.html"],
    "next.js": ["next.config.js", "next.config.ts", "app/layout.tsx", "pages/_app.tsx", "pages/_app.js"],
    "express": ["app.js", "server.js", "routes/", "middleware/"],
    "vue": ["vue.config.js", "src/App.vue", "src/main.ts", "src/main.js"],
    "angular": ["angular.json", "src/app/app.module.ts", "src/main.ts"],
    "svelte": ["svelte.config.js", "src/App.svelte", "src/routes/"],
    "spring": ["pom.xml", "build.gradle", "src/main/java/"],
    "rails": ["Gemfile", "config/routes.rb", "app/controllers/"],
    "laravel": ["artisan", "composer.json", "routes/web.php"],
    "go": ["go.mod", "main.go"],
    "rust": ["Cargo.toml", "src/main.rs"],
}


def detect_framework(repo_path: str) -> str | None:
    root_entries = set(os.listdir(repo_path))
    best_framework = None
    best_score = 0

    for framework, markers in _FRAMEWORK_MARKERS.items():
        score = 0
        for marker in markers:
            marker_path = os.path.join(repo_path, marker)
            if os.path.exists(marker_path):
                score += 1
            elif marker in root_entries:
                score += 1
        if score > best_score:
            best_score = score
            best_framework = framework

    if best_score <= 0:
        return None
    if best_framework == "go" and best_score == 1:
        if "pyproject.toml" in root_entries or "setup.py" in root_entries:
            return None
    if best_framework == "rust" and best_score == 1:
        if "package.json" in root_entries:
            return None
    return best_framework


def collect_all_files(repo_path: str, prefix: str = "") -> list[str]:
    files: list[str] = []
    try:
        entries = sorted(os.listdir(repo_path))
    except PermissionError:
        return files
    for entry in entries:
        if entry in SKIP_DIRS:
            continue
        full = os.path.join(repo_path, entry)
        rel = f"{prefix}/{entry}" if prefix else entry
        if os.path.isfile(full):
            files.append(rel)
        elif os.path.isdir(full):
            files.append(rel + "/")
            files.extend(collect_all_files(full, rel))
    return files


def read_file(path: str, max_lines: int = 100) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line.rstrip("\n"))
            return "\n".join(lines)
    except (OSError, UnicodeDecodeError):
        return None
