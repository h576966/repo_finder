# Source Scout

Local-first MCP server and CLI for finding reusable Next.js / React / TypeScript
UI code in public GitHub repositories.

The current direction is a **catalog-first reuse layer**, not generic GitHub
search. The system scouts candidate repositories, stores reproducible local
snapshots by commit SHA, extracts deterministic file-level evidence, and exposes
small source bundles to coding agents.

See `docs/source-scout-direction.md` for the full design direction.

## Prerequisites

- Python 3.11+
- GitHub personal access token for public repository access
- LM Studio for local Gemma/FastContext profiling

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
$env:GITHUB_TOKEN = "ghp_your_token_here"
$env:LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
$env:SOURCE_SCOUT_GEMMA_MODEL = "google/gemma-4-12b-qat"
$env:SOURCE_SCOUT_FASTCONTEXT_MODEL = "fastcontext-1.0-4b-rl"
$env:SOURCE_SCOUT_LMSTUDIO_TIMEOUT = "120"
```

## Catalog Workflow

```powershell
source-scout scout --domain nextjs-ui --limit 500
source-scout qualify --limit 100
source-scout lmstudio-status --smoke-test
source-scout profile --limit 30
source-scout evidence --capability data-table --limit 30
source-scout assess --candidate-id <asset_id> --task "Find a reusable route handler"
source-scout eval --suite ui-reuse --top-k 5
source-scout serve-mcp
```

## Task-Specific Assessment

`source-scout assess` turns one catalog candidate into a task-specific reuse
assessment:

```powershell
source-scout assess --candidate-id <asset_id> --task "Find a reusable route handler" --fastcontext-policy auto --max-evidence-rounds 1
```

Responsibilities stay split:

- Deterministic code validates paths, line ranges, commit SHA, evidence hashes,
  scoring, verdicts, and persistence.
- FastContext only scouts for additional file/line evidence. It never scores or
  decides reusability.
- Gemma interprets the validated evidence for the task, returns dimensions and
  evidence-linked reasons, and a model `recommended_verdict`. It never outputs
  the final score.

`recommended_verdict` is Gemma's model recommendation. `final_verdict` and
`reuse_score` are deterministic Source Scout outputs after evidence coverage
and blocker gates are applied.

Policy modes:

- `never`: use deterministic evidence only.
- `auto`: assess deterministic evidence first, then run one focused FastContext
  refinement only when Gemma asks for medium/high-priority FastContext evidence.
- `always`: attempt one FastContext refinement before the final assessment,
  unless `--max-evidence-rounds 0` is set.

Assessment evidence is commit-pinned and stored as a validated ledger with
content hashes. License metadata from GitHub is kept as passive context only.
Source Scout finds and assesses useful source; license review is outside scoring
and left to the user when needed.

Assessment calibration uses a mocked golden suite so assessor behavior can be
checked without live model variability:

```powershell
source-scout eval-assess --suite assessment-smoke --label local-v1
```

The report tracks verdict match rate, cache hits, repair counts, FastContext
attempt/completion/error counts, average reuse score, and evidence coverage.
See `docs/assessment-report-review.md` for a short field-by-field review guide.

## Standalone Local Exploration

FastContext can also explore the local project you are already working in. This
is separate from the catalog pipeline and does not write catalog rows:

```powershell
source-scout fastcontext-status --smoke-test
source-scout explore-local --project-path . --task "Find where MCP tools are registered" --max-turns 6
source-scout explore-local --project-path . --task "Find where MCP tools are registered" --trace-path .source_scout\fastcontext_traces\mcp-tools.json
source-scout eval-local-explore --suite source-scout --max-turns 6 --label local-fastcontext-check
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
`evals/golden/local_explore_source_scout_v1.json`. It measures expected file/line
hits, file/line precision and recall, unexpected or invalid citations, runtime,
tool calls, citation budget violations, and a simple manual-search proxy. The
current cleanup baseline is:

```powershell
source-scout eval-local-explore --suite source-scout --max-turns 6 --label cleanup-docs-v1
```

That run passed with `21/21` completed tasks, `path_hit_rate=0.7619`,
`line_overlap_rate=0.6190`, `average_citation_count=2.6667`, and zero invalid,
unsupported, or over-budget citations. Add personal repos by giving tasks an
absolute `project_path` or an env var-expanded path such as `%MY_NEXTJS_REPO%`.

Generated catalog data is stored under `.source_scout/` by default:

```text
.source_scout/
  cache.duckdb
  repos/
  bundles/
  logs/
```

Set `SOURCE_SCOUT_HOME` to use a different local storage directory.

## MCP Tools

Default tools:

| Tool | Purpose |
|------|---------|
| `find_reusable_code(task, project_path=None, max_repos=3)` | Return shortlisted reusable candidates, each with `task_signature`, evidence paths, and adaptation notes. |
| `assess_reusable_code(candidate_id, task, fastcontext_policy="auto", max_evidence_rounds=1, force=False)` | Assess one candidate for a task using the same structured result as `source-scout assess`. |
| `get_source_bundle(candidate_id, task_signature)` | Copy recommended files/config into a local bundle and write a manifest tied to the original task. |
| `record_reuse_outcome(candidate_id, task_signature, outcome, notes=None)` | Track selected, integrated, or rejected candidates against the original task. |
| `explore_local_code(task, project_path, max_turns=6)` | Use FastContext to find relevant files and line ranges in a local project without catalog writes. |

Legacy generic GitHub tools are hidden by default. Set
`SOURCE_SCOUT_ENABLE_LEGACY_TOOLS=1` only for debugging older behavior.

## LM Studio

This project is optimized for local LM Studio on Windows. Useful commands:

```powershell
lms ls
lms ps
lms server status
lms server start
Invoke-RestMethod http://127.0.0.1:1234/v1/models
source-scout lmstudio-status --smoke-test
source-scout lmstudio-status --load-gemma --smoke-test
source-scout fastcontext-status --load-model --smoke-test
```

Default local model IDs:

```text
Gemma:       google/gemma-4-12b-qat
FastContext: fastcontext-1.0-4b-rl
```

`source-scout profile` uses Gemma to store JSON profiles on repository cards.
FastContext supports read-only local exploration and evidence refinement through
the local LM Studio server.

### Recommended LM Studio Gemma preset

Use Source Scout's Gemma load helper before `profile` or `assess`:

```powershell
source-scout lmstudio-status --load-gemma --gemma-context-length 32768 --gemma-gpu max --smoke-test
```

This runs `lms load google/gemma-4-12b-qat --context-length 32768 --gpu max
--identifier google/gemma-4-12b-qat` when Gemma is missing or loaded with a
smaller context. The 32k context leaves headroom for task-specific assessment
prompts that include repository metadata, the evidence ledger, Gemma profile
data, and JSON completion. Source Scout's default LM Studio timeout is `120`
seconds because local Gemma/FastContext calls can exceed 30 seconds on real
assessment prompts.

### Recommended LM Studio FastContext preset

Use Source Scout's load helper as the default starting point:

```powershell
source-scout fastcontext-status --load-model --context-length 65536 --gpu max --smoke-test
```

This runs `lms load fastcontext-1.0-4b-rl --context-length 65536 --gpu max
--identifier fastcontext-1.0-4b-rl`, then checks that the model is downloaded,
loaded, and able to complete a smoke request.

Recommended LM Studio UI settings for this machine:

- Context length: `65536` for normal exploration. Raise it only when a task needs
  very large context.
- GPU offload: `max`.
- Parallel/concurrent predictions: `1` while using Source Scout from Codex.
- Temperature: `0.0` to `0.1`.
- Keep model in memory: enabled.
- Flash Attention: enabled.
- Qwen/FastContext thinking: disabled for tool-call requests. Source Scout sends
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
    "source-scout": {
      "command": "C:\\AI\\Dev\\source_scout\\.venv\\Scripts\\python.exe",
      "args": ["-m", "source_scout", "serve-mcp"],
      "env": {
        "PYTHONPATH": "C:\\AI\\Dev\\source_scout\\src",
        "SOURCE_SCOUT_HOME": "C:\\AI\\Dev\\source_scout\\.source_scout"
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

`pytest -q` runs the current Source Scout product path and excludes live
GitHub integration tests plus legacy generic-repo tests by default. Run those
only when needed:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m integration
.\.venv\Scripts\python.exe -m pytest -q -m legacy
```

Corpus quality check:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_checks.py
```

Golden catalog evals:

```powershell
source-scout eval --suite ui-reuse --top-k 5 --label local-ui-check
source-scout eval --suite nextjs-backend --top-k 5 --label local-backend-check
source-scout eval-local-explore --suite source-scout --max-turns 6 --label local-fastcontext-check
source-scout eval-assess --suite assessment-smoke --label local-assessment-check
```

Eval reports are written to `.source_scout/eval_runs/<suite_id>/`. They measure
top-1/top-3/top-5 hits, MRR, avoid-repo violations, and evidence constraint
failures against tracked golden tasks in `evals/golden/`. Local exploration eval
reports are written to `.source_scout/local_explore_eval_runs/<suite_id>/`.

## Project Structure

```text
src/source_scout/
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
