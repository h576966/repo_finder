# Source Scout

Local-first MCP server and CLI for finding reusable TypeScript,
JavaScript, Python, AI/data, Next.js, Node, and React source in public GitHub
repositories.

Source Scout optimizes one precision-first reuse loop:

```text
find_reusable_code -> assess_reusable_code -> get_source_bundle
```

Catalog retrieval can abstain when no candidate clears the versioned relevance
threshold. An optional read-only target-project profile improves deterministic
fit ranking. Assessment validates evidence at an exact commit, and only that
assessment can authorize a bounded source bundle. Deterministic code owns
scores, verdict gates, path and SHA validation, dependency closure, hashing,
persistence, manifests, and eval metrics. The configured assessment model
interprets validated evidence; the exploration harness only finds file and line
evidence. DeepSeek V4 Flash is the default model for both roles.

Version 0.2.0 intentionally replaces the bundle MCP contract with
`get_source_bundle(assessment_id)`. Callers using the 0.1.x candidate/task
signature form must reassess before creating a new bundle.

See `docs/source_scout_direction.md` for the current product direction and
`docs/complexity-budget.md` for scope boundaries and model role rules.

The installed CLI exposes both `source-scout` and `source_scout`; examples use
`source-scout`. The Python module/package remains `source_scout`.

On Windows, other local applications can invoke `source-scout.cmd` from the
repository root. The launcher resolves the project virtual environment relative
to itself, so it does not depend on the application's working directory. A PATH
shim can use `SOURCE_SCOUT_ROOT` when the checkout lives elsewhere.

## Prerequisites

- Python 3.11+
- GitHub personal access token for public repository access
- A DeepSeek API key

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
$env:GITHUB_TOKEN = "ghp_your_token_here"
$env:DEEPSEEK_API_KEY = "your_deepseek_api_key"
# Optional request timeout; 120 seconds is the default.
$env:SOURCE_SCOUT_MODEL_TIMEOUT = "120"
```

## Catalog Workflow

```powershell
source-scout scout --domain personal-code --limit 500
source-scout qualify --limit 100
source-scout model-status --smoke-test
source-scout profile --limit 30
source-scout evidence --domain personal-code --limit 100
source-scout assess --candidate-id <asset_id> --task "Find a reusable route handler" --project-path .
source-scout eval --suite ui-reuse --top-k 5
source-scout serve-mcp
```

`personal-code` is the default discovery domain. It is intentionally opinionated
for personal TS/JS/Python work: AI/local-AI harnesses, data pipelines, Next.js,
Node services, React UI, RAG/retrieval, eval harnesses, Python APIs, and Python
CLI tools. The older `nextjs-ui` domain remains available for focused UI-only
catalog runs.

## Task-Specific Assessment

`source-scout assess` turns one catalog candidate into a task-specific reuse
assessment:

```powershell
source-scout assess --candidate-id <asset_id> --task "Find a reusable route handler" --project-path . --fastcontext-policy auto --max-evidence-rounds 1
```

`--project-path` is optional. When supplied, Source Scout reads manifests and
source layout without executing project code, stores only canonical profile
facts, and includes the profile fingerprint in task signatures and assessment
caching. Use the same target path for `find_reusable_code` and
`assess_reusable_code`.

Responsibilities stay split:

- Deterministic code validates paths, line ranges, commit SHA, evidence hashes,
  scoring, verdicts, bounded file access, traces, manifests, and persistence.
- Deterministic retrieval and target-fit scoring choose which candidates clear
  the shortlist; model reranking is not part of the active baseline.
- The exploration harness only scouts for additional file/line evidence. It
  never scores or decides reusability.
- The assessment role interprets the validated evidence for the task, returns dimensions and
  evidence-linked reasons, and a model `recommended_verdict`. It never outputs
  the final score.

`recommended_verdict` is the model recommendation. `final_verdict` and
`reuse_score` are deterministic Source Scout outputs after evidence coverage
and blocker gates are applied.

Policy modes:

- `never`: use deterministic evidence only.
- `auto`: assess deterministic evidence first, then run one focused FastContext
  refinement only when the assessment role asks for medium/high-priority evidence.
- `always`: attempt one FastContext refinement before the final assessment,
  unless `--max-evidence-rounds 0` is set.

Assessment evidence is commit-pinned and stored as a validated ledger with
content hashes. License metadata from GitHub is kept as passive context only.
Source Scout finds and assesses useful source; license review is outside scoring
and left to the user when needed.

## Assessment-Gated Bundles

Call `get_source_bundle` with the `assessment_id` returned by
`assess_reusable_code`. `select` creates a normal bundle; `inspect` creates a
visibly marked inspection bundle with warnings. `reject` and
`insufficient_evidence` fail closed. Stale assessment schema/analyzer versions,
snapshot or commit mismatches, and assessments without validated adaptation
source paths are also rejected.

Bundles are published atomically under:

```text
.source_scout/bundles/<candidate_id>/<assessment_id>/
```

Required seeds come from assessed adaptation source paths. Source Scout follows
local Python and JS/TS runtime imports to depth two, then applies limits of five
supporting files, ten files total, and 512 KiB. A `select` bundle must have a
complete required relative-import closure; an `inspect` bundle may retain
explicit unresolved-import or truncation warnings.

`bundle.json` uses `source-bundle-v2`. It records assessment/model/prompt/schema
provenance, target-profile fingerprint, verdict and mode, required/optional
files with selection reasons, closure diagnostics, repository URL and exact
commit, source permalinks, file hashes, SPDX information, dependency
constraints, warnings, and total bytes. Existing task-signature bundle
directories are left untouched.

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
source-scout model-status --smoke-test
source-scout explore-local --project-path . --task "Find where MCP tools are registered" --max-turns 7
source-scout explore-local --project-path . --task "Find where MCP tools are registered" --trace-path .source_scout\fastcontext_traces\mcp-tools.json
source-scout eval-local-explore --suite source-scout --max-turns 7 --label local-fastcontext-check
```

Use this when relevant files are unknown and Codex would otherwise spend time on
broad `grep`/read loops, when a task needs multi-file tracing, or when direct
`rg` does not find enough context. Prefer direct `rg` for exact files, exact
symbols, commands, test names, config keys, and tiny questions. The exploration
harness uses the configured model API with read-only `Read`, `Glob`, and `Grep`
tools, then returns file and line citations. Codex still reads the cited files,
edits, and runs tests. If the API is unavailable, fall back to `rg`.

The default local exploration budget is currently seven turns. Use `--max-turns 8`
when a first result is incomplete or when calibrating deeper local exploration.
`--max-turns 12` is reserved for deep trace tasks, not normal development.

FastContext output is intentionally compact. Final answers are limited to at
most three citations across at most three files, with a target of one or two
tight ranges. After FastContext returns, read only the top one or two ranges
first with 30-80 line windows, batch independent narrow reads, and do not repeat
broad repository-wide searches for the same question. The harness prefers
citation IDs from observed tool results, retries once when the model
over-selects, and caps fallback observations so broad supporting ranges do not
look like real success.

The local exploration eval suite lives at
`evals/golden/local_explore_source_scout_v1.json`. It measures expected file/line
hits, file/line precision and recall, unexpected or invalid citations, runtime,
tool calls, citation budget violations, and a simple manual-search proxy. Run
the current cleanup verification with:

```powershell
source-scout eval-local-explore --suite source-scout --max-turns 7 --label cleanup-verify
source-scout eval-local-explore --suite source-scout --max-turns 7 --task-timeout-seconds 60 --progress
```

Reports are written under `.source_scout/local_explore_eval_runs/`; treat the
latest report as the source of current metrics. Add personal repos by giving
tasks an absolute `project_path` or an env var-expanded path such as
`%MY_NEXTJS_REPO%`.

The local personal Next.js suite for Ernaering can be run with:

```powershell
source-scout eval-local-explore --suite ernaering --max-turns 7 --label ernaering-local-check
```

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
| `find_reusable_code(task, project_path=None, max_repos=3)` | Return only candidates above the relevance threshold, with target-fit facts when a project is supplied; an empty result is a valid abstention. |
| `assess_reusable_code(candidate_id, task, fastcontext_policy="auto", max_evidence_rounds=1, force=False, project_path=None)` | Assess validated evidence for one candidate; reuse the same optional target project used during find. |
| `get_source_bundle(assessment_id)` | Validate a current `select`/`inspect` assessment and atomically publish its dependency-aware manifest-v2 bundle. |
| `record_reuse_outcome(candidate_id, task_signature, outcome, notes=None)` | Track selected, integrated, or rejected candidates against the original task. |
| `explore_local_code(task, project_path, max_turns=7)` | Use FastContext to find relevant files and line ranges in a local project without catalog writes. |

## Model API

Assessment, repository profiling, and exploration all use the hosted
`deepseek-v4-flash` model through DeepSeek's native, stateless Responses API at
`https://api.deepseek.com/responses`. The current documented deployment behind
that rolling API alias is `DeepSeek-V4-Flash-0731`; requests must still use
`deepseek-v4-flash` as the model ID.

Structured assessment and final exploration turns use Responses
`text.format` with JSON Schema. Exploration uses native function calls and
replays the complete response output with `function_call_output`, because
DeepSeek does not support `previous_response_id`. Thinking is disabled with
`reasoning={"effort":"none"}` for predictable tool replay, latency, and cost.
Transient failures use the OpenAI SDK's bounded retry policy; authentication and
other non-retryable errors surface directly. Traces retain the response ID,
returned model, token usage, and latency. DeepSeek Responses currently does not
return a documented `system_fingerprint`.

See the official [Responses API guide](https://api-docs.deepseek.com/guides/responses_api/)
and [Create Response reference](https://api-docs.deepseek.com/api/create-response/).

Check JSON assessment output and a native exploration tool call with:

```powershell
source-scout model-status --smoke-test
```

Set `DEEPSEEK_API_KEY` in the environment inherited by the CLI or MCP process.
For a persistent Windows user-level variable shared by projects, run this once
and then restart terminals, Codex, and MCP hosts so new processes inherit it:

```powershell
[Environment]::SetEnvironmentVariable(
  "DEEPSEEK_API_KEY",
  "your_deepseek_api_key",
  "User"
)
```

Do not commit API keys to the repository. Assessment prompts and exploration
tool results can contain repository source and are sent to DeepSeek. Do not run
model-backed commands for material that must remain offline.

MCP config:

```json
{
  "mcpServers": {
    "source_scout": {
      "command": "<repo-root>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "source_scout", "serve-mcp"],
      "env": {
        "PYTHONPATH": "<repo-root>\\src",
        "SOURCE_SCOUT_HOME": "<repo-root>\\.source_scout"
      }
    }
  }
}
```

Replace `<repo-root>` with your local Source Scout checkout path and make sure
the MCP process inherits `DEEPSEEK_API_KEY`.

## Local Checks

For normal local development:

```powershell
source-scout check
```

This runs the lightweight safe checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
```

`--with-local-explore-eval` runs the local FastContext eval and requires LM
Studio/FastContext to be available:

```powershell
source-scout check --with-local-explore-eval
```

Golden catalog evals:

```powershell
source-scout eval --suite ui-reuse --top-k 5 --label local-ui-check
source-scout eval --suite nextjs-backend --top-k 5 --label local-backend-check
source-scout eval --suite core-holdout --top-k 5 --label local-core-holdout
source-scout eval-reuse-loop --suite ui-reuse --top-k 3 --limit-tasks 3 --label local-loop-check
source-scout eval-local-explore --suite source-scout --max-turns 7 --label local-fastcontext-check
source-scout eval-assess --suite assessment-smoke --label local-assessment-check
```

Eval reports are written to `.source_scout/eval_runs/<suite_id>/`. They measure
top-1/top-3/top-5 hits, MRR, avoid-repo violations, and evidence constraint
failures against tracked golden tasks in `evals/golden/`. Local exploration eval
reports are written to `.source_scout/local_explore_eval_runs/<suite_id>/`.

Reuse-loop quality reports are written to
`.source_scout/reuse_loop_reports/<suite_id>/`. They run the active
`find_reusable_code -> assess_reusable_code -> get_source_bundle` shape over a
golden suite and separate positive retrieval from correct no-match abstention.
Reports include capability and target-fit correctness, expected commit/source
checks, assessment verdict/score/confidence/evidence coverage, bundle
required-file recall, allowed-file precision, unresolved imports, total bytes,
and SHA/hash failures. Correct no-match tasks must perform no assessment,
FastContext, or bundle work. By default the
command uses `--fastcontext-policy never --max-evidence-rounds 0` to keep the
report focused on shortlist quality plus DeepSeek assessment and bundle creation.
Treat failures as routing signals: missing expected repos point at
shortlist/scoring issues, assessment errors or low evidence coverage point at
assessment/evidence quality, and missing bundle files point at asset evidence or
snapshot issues.

Low-intent retrieval uses dependency-free BM25 over compact asset role cards.
It is limited to versioned role terms that pass the tracked regression and
keyword-ablation gates; recognized-intent ranking keeps the deterministic
baseline. Tree-sitter is not installed because the current JS/TS closure
fixtures do not demonstrate a structural miss.

## Project Structure

```text
src/source_scout/
  server.py          # FastMCP tools
  __main__.py        # CLI commands
  catalog.py         # Persistent DuckDB catalog
  pipeline.py        # Scout/qualify/gc workflow
  evidence.py        # Deterministic evidence extraction
  deepseek.py        # DeepSeek V4 Flash Responses API client
  fastcontext.py     # FastContext local exploration and evidence refinement
  local_explore_eval.py # FastContext local exploration eval runner
  profiler.py        # Model-backed repository-card profiling
  target_profile.py  # Read-only deterministic target-project profiling
  bundle_closure.py  # Bounded local import closure planning
  bundles.py         # Assessment-gated manifest-v2 bundle generation
  snapshotter.py     # Commit-SHA local snapshots
  github_client.py   # GitHub REST client
```

## Constraints

- Default discovery domain is the opinionated `personal-code` set for TS/JS,
  Python, AI/data, Next.js, Node, and React reuse. `nextjs-ui` remains available
  as a focused compatibility domain.
- Scout/qualify only accepts fresh repositories: created within 730 days,
  pushed within 180 days, public, not archived, not forks, not templates, not
  mirrors, and under the local size cap.
- Do not execute arbitrary cloned repository code.
- Analyze exact commit SHAs, not moving branch heads.
- Keep generated data local.
- Use local/manual review only; no external PR review services.
