# Development Notes

Useful implementation notes preserved from the old agent-specific setup.

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
- Store generated catalog data under `.repo_finder/`.
- Garbage-collect old snapshots through `repo-finder gc`.

## Model Runtime

- LM Studio is the intended local OpenAI-compatible endpoint.
- Default endpoint: `http://127.0.0.1:1234/v1`.
- Windows CLI: `C:\Users\Nikla\.lmstudio\bin\lms.exe`.
- Gemma is for JSON profiling/synthesis after deterministic evidence exists.
- FastContext is for evidence refinement over read-only `READ`, `GLOB`, and
  `GREP`-style tools, not general code generation.
- Standalone FastContext exploration is evaluated through
  `evals/golden/local_explore_repo_finder_v1.json` and
  `repo-finder eval-local-explore --suite repo-finder --max-turns 6`.
- Final FastContext evidence is budgeted to at most three citations across at
  most three files, with one or two tight ranges preferred.

Local status and smoke tests:

```powershell
lms ls
lms server status
lms server start
Invoke-RestMethod http://127.0.0.1:1234/v1/models
repo-finder lmstudio-status --smoke-test
```

Current local model defaults:

```text
REPO_FINDER_GEMMA_MODEL=google/gemma-4-12b-qat
REPO_FINDER_FASTCONTEXT_MODEL=fastcontext-1.0-4b-rl
```
