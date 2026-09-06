# Development notes — 2026-09-06

Install `.[all,dev]` in the trusted working copy and run `source-scout check`
before completion. The report records the actual tracked and non-ignored file
identity before and after checks. Rerun after edits. Do not run cloned source
code or use this runner as a generic verification engine for another project.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_implementation_references.py tests/test_catalog_processes.py tests/test_github_client.py -q --basetemp .source_scout/refs-test -o cache_dir=.source_scout/pytest-cache
.venv\Scripts\python.exe -m pytest tests/test_investigation_anchors.py tests/test_exploration_policy.py tests/test_sidecar.py tests/test_integration.py -q --basetemp .source_scout/integration-test -o cache_dir=.source_scout/pytest-cache
.venv\Scripts\python.exe -m source_scout check --format json
```

Use a fresh basetemp path for a new run. Catalog fixtures are opt-in. The autouse
test fixture supplies dummy model credentials, removes external API credentials
and blocks external socket connections, while retaining local MCP IPC. GitHub
and DeepSeek use HTTP mock transports in standard tests. No paid eval runs in
checks. CI runs on Windows with Python 3.12, installs dependencies, then runs the
same offline contracts. Windows is the supported platform; Linux compatibility
is outside the current verification scope.

Baseline `ec5266d78157b5c0a6c7dafc08b7cd34f25d7116` had 385 tests. The current
suite collects 245: exclusive assessment/scoring/bundling/refinement/eval tests
were retired, and product regressions now cover metadata false positives,
canonical CRLF/filter-safe blobs, bounded Unicode/one-line responses, cjs/cts,
unknown ecosystem fit, two-process add/find, lock release and rollback,
historical schema/ID/hash compatibility, retired GC, Windows long paths,
GitHub auth/failure/provenance, anchors and actual stdio across two worktrees.
Existing policy-before-key/source, path escape, stale evidence, deadlines, zero
retry and process termination contracts remain. Counts alone are not coverage.

`eval-navigation --suite source-scout` is an explicit paid navigation diagnostic,
not product acceptance. Its portable v2 fixture names current source relations;
invalid citations must be zero. Historical reuse suites, personal worktree paths
and `check --with-local-explore-eval` are removed. No live diagnostic was run.

Versioned contracts: implementation reference schema/index v2; investigation
report v3, evidence schema v3, prompt v5, analyzer v4; Git materialization v2.
Historic fields retain their original meaning. `explore-local` and
`fastcontext.explore_local_project` remain limited caller compatibility, with
the historical `project_path` result field; modern MCP inputs use `source_root`.

## Dependencies and official contracts checked 2026-09-06

No blanket upgrade: installed FastMCP 3.4.2, DuckDB 1.5.4, GitPython 3.1.50,
Pydantic 2.13.4, HTTPX 0.28.1 and OpenAI SDK 2.44.0 were retained. Development
versions observed: Ruff 0.15.20, mypy 2.1.0, pytest 9.1.1. Extras separate imports
and installation; a clean built wheel was installed without optional dependencies
and `check --help` succeeded with model/catalog/MCP packages absent.

[Codex plugin packaging](https://developers.openai.com/plugins/build/plugins)
and [skill documentation](https://learn.chatgpt.com/docs/build-skills) were checked
against actual CLI 0.153.0 discovery. This host reads the prior `.codex/skills`
location; the migration does not duplicate it under `.agents/skills`. The helper's
`mcpServers` wrapper works on this host. Personal marketplace relative source
paths resolved from the user directory in the actual installation, so the small
installer uses `~/plugins/source-scout`; discovery is the acceptance check.

[Serena's client guidance](https://oraios.github.io/serena/02-usage/030_clients.html)
was checked; the installed commit stays pinned. Do not infer its revision from
the banner, which on this host includes the caller checkout's Git revision.
Use installed `direct_url.json`. Two isolated stdio sessions returned different
symbol bodies for the same path in two worktrees, with correct callers. The
probe sets a workspace `UV_TOOL_DIR` and offline cached LSP dependencies; its first
sandboxed run diagnosed a uv tools-lock permission failure, then passed after
the tools directory was explicitly scoped. No LSP upgrade was necessary.

[DeepSeek Responses](https://api-docs.deepseek.com/api/create-response/) supports
the selected `deepseek-v4-flash` and stateless conversation replay. Its
[compatibility table](https://api-docs.deepseek.com/guides/responses_api/) says
`max_tool_calls` and `parallel_tool_calls` are ignored. Local request/tool/deadline
limits and replay therefore remain authoritative. No provider smoke check or
paid request was used to verify this documentation.

[GitHub REST version policy](https://docs.github.com/en/rest/about-the-rest-api/api-versions)
still supports `2022-11-28` through 2028-03-10. Retain that version intentionally;
moving to `2026-03-10` offers no required contract benefit here. Private Git
fetch credentials are independent of REST; real private Git fetch was not tested.
