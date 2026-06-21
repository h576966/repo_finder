# Repo Finder

Local-first MCP server and CLI for finding reusable Next.js / React / TypeScript
UI code in public GitHub repositories.

The current direction is a **catalog-first reuse layer**, not generic GitHub
search. The system scouts candidate repositories, stores reproducible local
snapshots by commit SHA, extracts deterministic file-level evidence, and exposes
small source bundles to coding agents.

See `docs/repo-finder-direction.md` for the full design direction.

## Prerequisites

- Python 3.11+
- GitHub personal access token for public repository access
- LM Studio for local Gemma/FastContext profiling

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:GITHUB_TOKEN = "ghp_your_token_here"
$env:LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
$env:REPO_FINDER_GEMMA_MODEL = "google/gemma-4-12b-qat"
$env:REPO_FINDER_FASTCONTEXT_MODEL = "fastcontext-1.0-4b-rl"
```

## Catalog Workflow

```powershell
repo-finder scout --domain nextjs-ui --limit 500
repo-finder qualify --limit 100
repo-finder lmstudio-status --smoke-test
repo-finder profile --limit 30
repo-finder evidence --capability data-table --limit 30
repo-finder serve-mcp
```

Generated catalog data is stored under `.repo_finder/` by default:

```text
.repo_finder/
  cache.duckdb
  repos/
  bundles/
  logs/
```

Set `REPO_FINDER_HOME` to use a different local storage directory.

## MCP Tools

Default tools:

| Tool | Purpose |
|------|---------|
| `find_reusable_code(task, project_path=None, max_repos=3)` | Return shortlisted reusable candidates with evidence paths and adaptation notes. |
| `get_source_bundle(candidate_id)` | Copy recommended files/config into a local bundle and write a manifest. |
| `record_reuse_outcome(candidate_id, outcome, notes=None)` | Track selected, integrated, or rejected candidates for future ranking. |

Legacy generic GitHub tools are hidden by default. Set
`REPO_FINDER_ENABLE_LEGACY_TOOLS=1` only for debugging older behavior.

## LM Studio

This project is optimized for local LM Studio on Windows. Useful commands:

```powershell
lms ls
lms server status
lms server start
Invoke-RestMethod http://127.0.0.1:1234/v1/models
repo-finder lmstudio-status --smoke-test
```

Default local model IDs:

```text
Gemma:       google/gemma-4-12b-qat
FastContext: fastcontext-1.0-4b-rl
```

`repo-finder profile` uses Gemma to store JSON profiles on repository cards.
FastContext is configured for status checks now and will be used for evidence
refinement after Gemma profile quality is measured.

Optional LM Studio MCP config:

```json
{
  "mcpServers": {
    "repo-finder": {
      "command": "C:\\AI\\Dev\\repo_finder\\.venv\\Scripts\\python.exe",
      "args": ["-m", "repo_finder", "serve-mcp"],
      "env": {
        "PYTHONPATH": "C:\\AI\\Dev\\repo_finder\\src",
        "REPO_FINDER_HOME": "C:\\AI\\Dev\\repo_finder\\.repo_finder"
      }
    }
  }
}
```

## Local Checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
```

Corpus quality check:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_checks.py
```

## Project Structure

```text
src/repo_finder/
  server.py          # FastMCP tools
  __main__.py        # CLI commands
  catalog.py         # Persistent DuckDB catalog
  pipeline.py        # Scout/qualify/gc workflow
  evidence.py        # Deterministic evidence extraction
  lmstudio.py        # Local LM Studio API adapter
  profiler.py        # Gemma repository-card profiling
  bundles.py         # Source bundle generation
  snapshotter.py     # Commit-SHA local snapshots
  github_client.py   # GitHub REST client
```

## Constraints

- First domain is Next.js / React / TypeScript UI reuse.
- Do not execute arbitrary cloned repository code.
- Analyze exact commit SHAs, not moving branch heads.
- Keep generated data local.
- Use local/manual review only; no external PR review services.
