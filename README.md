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
repo-finder eval --suite ui-reuse --top-k 5
repo-finder serve-mcp
```

## Standalone Local Exploration

FastContext can also explore the local project you are already working in. This
is separate from the catalog pipeline and does not write catalog rows:

```powershell
repo-finder fastcontext-status --smoke-test
repo-finder explore-local --project-path . --task "Find where MCP tools are registered" --max-turns 6
repo-finder explore-local --project-path . --task "Find where MCP tools are registered" --trace-path .repo_finder\fastcontext_traces\mcp-tools.json
repo-finder eval-local-explore --suite repo-finder --max-turns 6 --label local-fastcontext-check
```

Use this when relevant files are unknown and Codex would otherwise spend time on
broad `grep`/read loops. FastContext uses LM Studio's OpenAI-compatible tool
calling with read-only `Read`, `Glob`, and `Grep` tools, then returns file and
line citations. Codex still reads the cited files, edits, and runs tests. If LM
Studio or FastContext is unavailable, fall back to `rg`.

FastContext output is intentionally compact. Final answers are limited to at
most three citations across at most three files, with a target of one or two
tight ranges. The harness prefers citation IDs from observed tool results,
retries once when the model over-selects, and caps fallback observations so
broad supporting ranges do not look like real success.

The local exploration eval suite lives at
`evals/golden/local_explore_repo_finder_v1.json`. It measures expected file/line
hits, file/line precision and recall, unexpected or invalid citations, runtime,
tool calls, citation budget violations, and a simple manual-search proxy. The
current cleanup baseline is:

```powershell
repo-finder eval-local-explore --suite repo-finder --max-turns 6 --label cleanup-docs-v1
```

That run passed with `21/21` completed tasks, `path_hit_rate=0.7619`,
`line_overlap_rate=0.6190`, `average_citation_count=2.6667`, and zero invalid,
unsupported, or over-budget citations. Add personal repos by giving tasks an
absolute `project_path` or an env var-expanded path such as `%MY_NEXTJS_REPO%`.

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
| `find_reusable_code(task, project_path=None, max_repos=3)` | Return shortlisted reusable candidates, each with `task_signature`, evidence paths, and adaptation notes. |
| `get_source_bundle(candidate_id, task_signature)` | Copy recommended files/config into a local bundle and write a manifest tied to the original task. |
| `record_reuse_outcome(candidate_id, task_signature, outcome, notes=None)` | Track selected, integrated, or rejected candidates against the original task. |
| `explore_local_code(task, project_path, max_turns=6)` | Use FastContext to find relevant files and line ranges in a local project without catalog writes. |

Legacy generic GitHub tools are hidden by default. Set
`REPO_FINDER_ENABLE_LEGACY_TOOLS=1` only for debugging older behavior.

## LM Studio

This project is optimized for local LM Studio on Windows. Useful commands:

```powershell
lms ls
lms ps
lms server status
lms server start
Invoke-RestMethod http://127.0.0.1:1234/v1/models
repo-finder lmstudio-status --smoke-test
repo-finder fastcontext-status --load-model --smoke-test
```

Default local model IDs:

```text
Gemma:       google/gemma-4-12b-qat
FastContext: fastcontext-1.0-4b-rl
```

`repo-finder profile` uses Gemma to store JSON profiles on repository cards.
FastContext supports read-only local exploration and evidence refinement through
the local LM Studio server.

### Recommended LM Studio FastContext preset

Use Repo Finder's load helper as the default starting point:

```powershell
repo-finder fastcontext-status --load-model --context-length 65536 --gpu max --smoke-test
```

This runs `lms load fastcontext-1.0-4b-rl --context-length 65536 --gpu max
--identifier fastcontext-1.0-4b-rl`, then checks that the model is downloaded,
loaded, and able to complete a smoke request.

Recommended LM Studio UI settings for this machine:

- Context length: `65536` for normal exploration. Raise it only when a task needs
  very large context.
- GPU offload: `max`.
- Parallel/concurrent predictions: `1` while using Repo Finder from Codex.
- Temperature: `0.0` to `0.1`.
- Keep model in memory: enabled.
- Flash Attention: enabled.
- Qwen/FastContext thinking: disabled for tool-call requests. Repo Finder sends
  `chat_template_kwargs.enable_thinking=false` because LM Studio rejects tools
  with `Cannot combine structured output constraints with lazy grammar` when
  thinking is active.
- Structured Output: optional for smoke/simple JSON prompts. FastContext
  exploration uses tool calling instead of combining tools with structured
  output, and still keeps the robust JSON parser as fallback.

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

Golden catalog evals:

```powershell
repo-finder eval --suite ui-reuse --top-k 5 --label local-ui-check
repo-finder eval --suite nextjs-backend --top-k 5 --label local-backend-check
repo-finder eval-local-explore --suite repo-finder --max-turns 6 --label local-fastcontext-check
```

Eval reports are written to `.repo_finder/eval_runs/<suite_id>/`. They measure
top-1/top-3/top-5 hits, MRR, avoid-repo violations, and evidence constraint
failures against tracked golden tasks in `evals/golden/`. Local exploration eval
reports are written to `.repo_finder/local_explore_eval_runs/<suite_id>/`.

## Project Structure

```text
src/repo_finder/
  server.py          # FastMCP tools
  __main__.py        # CLI commands
  catalog.py         # Persistent DuckDB catalog
  pipeline.py        # Scout/qualify/gc workflow
  evidence.py        # Deterministic evidence extraction
  lmstudio.py        # Local LM Studio API adapter
  fastcontext.py     # FastContext local exploration and evidence refinement
  local_explore_eval.py # FastContext local exploration eval runner
  profiler.py        # Gemma repository-card profiling
  bundles.py         # Source bundle generation
  snapshotter.py     # Commit-SHA local snapshots
  github_client.py   # GitHub REST client
```

## Constraints

- First domain is Next.js / React / TypeScript UI reuse.
- Scout/qualify only accepts fresh repositories: created within 730 days,
  pushed within 180 days, public, not archived, not forks, not templates, not
  mirrors, and under the local size cap.
- Do not execute arbitrary cloned repository code.
- Analyze exact commit SHAs, not moving branch heads.
- Keep generated data local.
- Use local/manual review only; no external PR review services.
