# Source Scout

A small complement to Codex: compact, persisted local check results and an
optional read-only code investigator. Codex owns reasoning, changes, test
selection and final assessment. Use `rg` for exact text/files and Serena for
symbols/references when available. There is no mandatory tool sequence.

Python 3.11+ and the existing project virtual environment are sufficient for
the default flow. No GitHub or DeepSeek key is required. Both `source-scout`
and `source_scout` CLI names remain available, as does `python -m source_scout`.
The repository's `source-scout.cmd` resolves its own virtual environment.

## Local checks

From the trusted Source Scout working copy:

```powershell
.\.venv\Scripts\python.exe -m source_scout check
.\.venv\Scripts\python.exe -m source_scout check --format json
.\.venv\Scripts\python.exe -m source_scout check --timeout-seconds 300
```

The wrapper runs Ruff, mypy and pytest using the project's `.venv` Python
(falling back to the invoking Python if no `.venv` is present). Commands use
argument lists, never `shell=True`. It continues after independent failures.
Timeout/cancellation terminates the active process tree; cancellation marks
remaining checks `not_run`. It never automatically runs downloaded repositories.
Windows uses a job object and waits to launch the check until the process is
assigned; cleanup also covers descendants when `taskkill` is unavailable.

Default stdout is a short report. JSON stdout has no banners/progress; progress
goes to stderr. Every invocation keeps its own directory:

```text
.source_scout/checks/<run-id>/
  report.json
  ruff.stdout.log / ruff.stderr.log
  mypy.stdout.log / mypy.stderr.log
  pytest.stdout.log / pytest.stderr.log
  pytest.xml
  pytest-temp/ / pytest-cache/
```

Reports include schema/run ID, timestamps, argv, cwd, duration, exit code,
errors, parse/truncation flags and original log paths. Statuses distinguish
`passed`, `failed`, `error`, `timeout`, `cancelled` and `not_run`. Ruff JSON and
pytest JUnit XML are parsed; mypy uses a bounded text summary. Empty collection,
missing tools, collection errors and invalid structured results cannot pass.
Skip/xfail counts are separate; unavailable xpass information is `null`.
Full logs remain available even when summaries or parser inputs are truncated.

The report records Git HEAD and a content fingerprint of tracked and
non-ignored untracked files before/after checks. `success` requires all required
checks to pass and no detected change. Missing checkout identity fails closed.
This is not an atomic snapshot: ignored files and edits reverted between the
two observations are outside the fingerprint. A saved report does not verify
subsequent edits; run the command again. Logs are ignored by Git.

`check --with-local-explore-eval` remains an explicit paid-eval option and also
requires the exploration policy to be enabled. It is never part of normal checks.

## MCP profiles

```powershell
source-scout serve-mcp
source-scout serve-mcp --profile reuse
```

| Profile | Tools |
| --- | --- |
| `sidecar` (default) | `explore_local_code`, `model_status` |
| `reuse` (explicit legacy) | Above plus `find_reusable_code`, `assess_reusable_code`, `get_source_bundle`, `record_reuse_outcome` |

There is no MCP shell/check runner. Codex runs `source-scout check` in its normal
terminal. Normal CLI/check imports and sidecar startup do not initialize or
import the catalog. Disabled exploration/model-status return `disabled` without
model validation, network requests or source-context collection.

Project-local `.codex/config.toml` configures the Codex VS Code extension and
CLI, retaining other/global MCP settings. Source Scout uses a 270-second outer
MCP deadline, a 240-second inner exploration deadline and a 300-second client
timeout. Serena has a separate process and installation; it is not a Source
Scout dependency. See [setup and verified status](docs/source_scout_direction.md).

## Optional remote investigation

`.source-scout.toml` defaults to disabled. `SOURCE_SCOUT_REMOTE_EXPLORATION`
overrides project policy; only `1`, `true` or `yes` enables it. An inherited API
key does not activate it. To explicitly enable it in the current terminal:

```powershell
$env:SOURCE_SCOUT_REMOTE_EXPLORATION = 'true'
source-scout explore-local --project-path . --task 'Trace the unresolved caller contract' --reason 'rg and symbol references did not resolve the cross-file contract'
Remove-Item Env:SOURCE_SCOUT_REMOTE_EXPLORATION
```

For MCP, edit `SOURCE_SCOUT_REMOTE_EXPLORATION` in the source_scout environment
section of `.codex/config.toml` and restart that server. Changing another
terminal's environment does not affect an already-running MCP process.
The inherited `DEEPSEEK_API_KEY` is used without storing it in project config.

The existing `deepseek-v4-flash` client/model configuration is retained. The
investigator receives the bounded task and necessary read-only observations,
not the Codex conversation. The seven-turn default remains; `--max-turns` allows
1-12 for explicitly selected investigations. All response/finalization calls
share that cap, and optional model validation consumes a call. SDK retries are
zero for local exploration; errors return `unavailable` with no fallback model.
`SOURCE_SCOUT_EXPLORATION_DEADLINE_SECONDS` can lower the 240-second total limit.
Policy is checked before model configuration, validation or source collection.

Citations require the entire interval to have appeared in returned source text,
possibly across multiple reads. Grep context metadata and truncated lines do
not count. Observations carry content hashes; changed files are rejected as
stale. Sensitive filenames and paths, generated state and external symlinks
are excluded from source context, including seed-context/repo-map. Source text
is data, never authority to expand access.

Results stay compact (at most three citations). `incomplete`, `missing_context`,
`truncated` and `stop_reason` distinguish budget stops, failed finalization and
missing support. Small output is not a claim of full context coverage.

Each enabled run saves a versioned report and detailed trajectory under
`.source_scout/explorations/<run-id>/report.json`, including requested/returned
model, available input/cached-input/output usage, latency, known retries, reason
and stop cause. Cached input is part of input, not an additional total. Unknown
usage/cost is `null`; synthetic fallback records are not counted as requests.
`--trace-path` optionally writes an additional copy inside `.source_scout/`.
CLI exit status is nonzero for disabled, unavailable or incomplete exploration.
Details live locally, outside ordinary MCP output.

This opt-in governs additional Source Scout calls. It does not describe the
whole Codex workflow as offline. No verified cost-saving percentage is claimed.

## Preserved legacy catalog

Catalog feature development is frozen. Existing catalog, snapshots and bundles
are preserved; no migration/deletion is performed. The explicit reuse MCP
profile retains `find -> assess -> bundle -> outcome`. Deterministic code owns
scores, verdict gates, source/SHA validation and persistence; legacy models
interpret validated evidence. Assessment-gated, commit-pinned bundles remain.

Existing CLI commands remain available: `scout`, `qualify`, `evidence`, `assess`,
`profile`, `audit`, `refine-evidence`, `gc`, `eval`, `eval-assess`,
`eval-reuse-loop`, `eval-local-explore`. Legacy commands can require GitHub and
DeepSeek credentials and retain their existing model behavior. They are
explicit operations outside the sidecar's local-exploration policy. Do not
execute downloaded source code. Catalog state stays in `.source_scout/` or the
existing `SOURCE_SCOUT_HOME` location.

To expose catalog tools again, run `serve-mcp --profile reuse`. For Codex, also
remove/extend the sidecar `enabled_tools` list in `.codex/config.toml` and restart
the server. To restore the previous global MCP settings for this project,
remove only the project-local `source_scout` and `serena` tables (including their
environment tables); the global file was not changed. The server's new safe
default still requires `--profile reuse` for catalog tools. Existing data needs
no rollback. Reverting the code changes restores the previous CLI/report
contracts if needed; no automatic commit, push or PR is created.
