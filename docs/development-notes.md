# Development Notes

Useful implementation notes for the current Source Scout product path.

## FastMCP

- Define tools with `@mcp.tool()` and use `annotations={"readOnlyHint": True}`
  only for tools that do not write files, mutate the catalog, or record outcomes.
- Prefer `Annotated[..., Field(...)]` for tool parameters so MCP clients get clear
  schemas.
- Return project dataclasses from `models.py` for structured tool output.
- Use `ToolError` for user-correctable validation errors.
- Use structured runtime errors for recoverable system failures such as rate
  limits.

## GitHub API

- Repository search uses `GET /search/repositories` with query qualifiers such as
  `language:`, `topic:`, `pushed:`, `archived:false`, `is:public`, `size:`, and
  `in:name,description,topics,readme`.
- Qualification rejects archived, private, stale, mirrored, oversized,
  docs-only/empty, lockfile-only, and generated/vendor-heavy repositories.
- Authenticated search has tighter search-specific limits than normal REST calls;
  keep scouting offline/batched rather than per MCP request.
- Treat GitHub language metadata as a signal only. Confirm stack via manifests,
  config files, and local source snapshots.
- Resolve and store the exact default-branch commit SHA before analysis.

## Local Snapshots

- Clone or fetch by commit SHA, not moving branch names.
- Never execute code from cloned repositories.
- Store generated catalog data under `.source_scout/`.
- Garbage-collect old snapshots through `source-scout gc`.

## Reuse Loop Contracts

- Preserve the order `find_reusable_code -> assess_reusable_code ->
  get_source_bundle`.
- Pass the same optional `project_path` to find and assess. Target profiling is
  read-only: do not execute code, follow directory symlinks, persist source
  text, or store the absolute project path.
- `get_source_bundle` accepts only an `assessment_id`. Do not add a legacy
  candidate/task-signature bridge.
- Only current `select` and `inspect` assessments can create bundles. Old-schema
  assessments must be rerun.
- New bundles live at
  `.source_scout/bundles/<candidate_id>/<assessment_id>/`; legacy bundle
  directories remain readable and untouched.
- Manifest `source-bundle-v2` records exact commit/provenance, required and
  optional files, import-closure diagnostics, source hashes/permalinks, warnings,
  dependency constraints, and total bytes.

## Model Runtime

- DeepSeek V4 Flash is the only model runtime. Every model-backed role uses the
  rolling API alias `deepseek-v4-flash`.
- Base URL: `https://api.deepseek.com`; endpoint: `/responses`.
- Structured assessment calls and every exploration request use Responses
  `text.format` with JSON Schema.
  Exploration replays response output items and `function_call_output` because
  the API is stateless and does not support `previous_response_id`.
- Requests use `reasoning={"effort":"none"}` and temperature `0`. Non-retryable
  errors surface directly; transient retries are bounded by the SDK.
- The assessment role handles JSON profiling/synthesis after deterministic evidence exists.
- The exploration role handles evidence refinement over read-only `READ`, `GLOB`, and
  `GREP`-style tools, not general code generation.
- Standalone FastContext exploration is evaluated through
  `evals/golden/local_explore_source_scout_v1.json` and
  `source-scout eval-local-explore --suite source-scout --max-turns 7`.
- Final FastContext evidence is budgeted to at most three citations across at
  most three files, with one or two tight ranges preferred.

## Prompt Maintenance

- Keep production prompts in source code and review them like application logic.
- Bump the relevant `PROMPT_VERSION` whenever prompt behavior changes.
- Prefer short, outcome-first prompts with explicit evidence rules, retrieval
  budgets, validation rules, and output schema expectations.
- Preserve all returned Responses output items before appending matching
  `function_call_output` items. Every function call must receive an output,
  including calls skipped by the local execution cap.
- Do not add broad process instructions unless tests or evals show they improve
  retrieval or assessment quality.

API status and smoke tests:

```powershell
source-scout model-status --smoke-test
```

Status failures include an `error_type`. A sandbox-only `connection` failure
must be retried once with approved network access; configuration and HTTP
errors should be handled directly without a network escalation retry.

Default test runs cover catalog, assessment, the DeepSeek Responses contract,
and exploration:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Required model environment:

```text
DEEPSEEK_API_KEY=<inherited by the CLI or MCP process>
# Optional:
SOURCE_SCOUT_MODEL_TIMEOUT=120
```
