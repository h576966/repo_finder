# Source Scout

A small complement to Codex: compact, persisted local check results and an
optional read-only code investigator. Codex owns reasoning, changes, test
selection and final assessment. Use `rg` for exact text/files and Serena for
symbols/references when available. There is no mandatory tool sequence.

Python 3.11+ and the existing project virtual environment are sufficient for
the default flow. No GitHub or DeepSeek key is required. Both `source-scout`
and `source_scout` CLI names remain available, as does `python -m source_scout`.
The repository's `source-scout.cmd` resolves its own virtual environment.

## Personal implementation references

The primary reuse path is an explicit personal or curated collection. It does
not scan disks or GitHub accounts, execute source repositories, invoke a model,
or depend on the legacy capability/assessment/bundle pipeline.

```powershell
# Snapshot HEAD, including only committed bytes.
source-scout reference-add --source C:\code\retry-patterns --kind personal

# A revision may be selected explicitly. GitHub commits must be full SHAs.
source-scout reference-add --source https://github.com/acme/retry-patterns --kind curated --commit <sha>

source-scout reference-find --task "bounded retry with decorrelated jitter" --project-path .
source-scout reference-context --candidate-id <candidate-id> --task "bounded retry with decorrelated jitter" --project-path .
```

`reference-add` stores origin, selection kind, repository facts, an exact clean
commit snapshot, observed license data and stable file identities. Explicitly
selected private, archived, fork and template repositories are accepted; those
facts are reported rather than filtered. Local origins without a verified
GitHub remote never receive a fabricated permalink.

`reference-find` returns at most three results. It searches source, paths,
identifiers and manifests with deterministic BM25 evidence. An absolute
task-evidence gate runs before personal priority and advisory target fit, so a
weak singleton cannot pass through relative normalization. The result explicitly
abstains when evidence is insufficient. `reference-context` revalidates the
snapshot, commit and full-file hash, then returns bounded exact line ranges,
snippet hashes, manifests, target facts, license observations and commit-pinned
GitHub links where verified.

Local add/find/context needs no API key or network. GitHub access is a separate
explicit operation; public access can be anonymous and private access requires
`GITHUB_TOKEN`:

```powershell
source-scout reference-github-search --task "bounded retry with decorrelated jitter" --max-results 3
```

Fallback inspects at most three results at resolved commits and does not persist
them. Its output distinguishes no matches from network failure and prints the
separate pinned `reference-add` command for a chosen repository.

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
requires explicit `on` mode for the legacy unclassified eval tasks. It is never
part of normal checks. Its path/line hit metric measures navigation, not finished
patch quality, overall Codex cost or preservation of final-result quality.

## MCP profiles

```powershell
source-scout serve-mcp
source-scout serve-mcp --profile references
source-scout serve-mcp --profile reuse
```

| Profile | Tools |
| --- | --- |
| `sidecar` (default) | `explore_local_code` |
| `references` (opt-in) | `find_reuse_references`, `get_reuse_context` |
| `reuse` (explicit legacy) | Above plus `model_status`, `find_reusable_code`, `assess_reusable_code`, `get_source_bundle`, `record_reuse_outcome` |

There is no MCP shell/check runner. Codex runs `source-scout check` in its normal
terminal. Normal CLI/check imports and sidecar startup do not initialize or
import the catalog. Disabled exploration/model-status return `disabled` without
model validation, network requests or source-context collection.
`source-scout model-status` remains an explicit CLI/debug model-health operation;
`--smoke-test` explicitly selects model requests. These debug operations are
available in selective/on mode; off prevents them. They are outside normal routing.

Project-local `.codex/config.toml` configures the Codex VS Code extension and
CLI, retaining other/global MCP settings. Source Scout uses a 270-second outer
MCP deadline, a 240-second inner exploration deadline and a 300-second client
timeout. Serena has a separate process and installation; it is not a Source
Scout dependency. See [setup and verified status](docs/source_scout_direction.md).

## Optional remote investigation

`.source-scout.toml` commits `mode = "selective"` under `[remote_exploration]`.
Policy is read on every invocation before model configuration, API-key access,
network validation or seed/source collection:

| Mode | Investigator contract |
| --- | --- |
| `off` | Returns disabled with no source/model/network work. |
| `selective` | Task, concrete reason, approved use case, and at least one attempted local method. |
| `on` | Task and concrete reason; classification/local attempts may be omitted. All safety and budget limits remain. |

| Selective use case | Unresolved question after local navigation |
| --- | --- |
| `cross_file_contract` | A definition/reference chain is known, but the contract between its files/modules remains unclear. |
| `indirect_runtime_flow` | Callbacks, registration, dependency injection, events or similar dispatch prevent direct navigation from explaining runtime flow. |
| `ambiguous_ownership` | Local search found multiple plausible components; ownership of the behavior remains unclear. |
| `architecture_trace` | A concrete relation spans several subsystems and direct navigation cannot establish it. |

Local methods are `rg`, `direct_read` and `serena`. They record caller-declared attempts, not proof of
tool execution. Serena is not required: appropriate rg/direct reads suffice.
For an unresolved contract after local navigation:

```powershell
source-scout explore-local --project-path . --task 'Trace the unresolved caller contract' --reason 'rg and direct reads left the return contract unresolved' --use-case cross_file_contract --attempted-local-method rg --attempted-local-method direct_read
```

MCP uses the same fields (`use_case`, `attempted_local_methods`, `reason`). The
project-local Codex configuration has no permanent mode override. Ordinary
selective calls require no config edit or server restart. The inherited
`DEEPSEEK_API_KEY` is used without storing it in project config.

`SOURCE_SCOUT_REMOTE_EXPLORATION=off|selective|on` overrides project policy.
For example, `$env:SOURCE_SCOUT_REMOTE_EXPLORATION = 'off'` forces off in the
current terminal; `Remove-Item Env:SOURCE_SCOUT_REMOTE_EXPLORATION` restores
project policy. Another terminal's environment does not affect a running MCP
process. Legacy `false/0/no` map to off and `true/1/yes` map to on. In project
TOML, legacy `enabled = false/true` maps to off/on; an explicit `mode` wins.
Missing/unreadable/invalid policy or invalid override fails closed to off.
An API key never overrides policy. Unknown use cases/local methods are rejected,
including when supplied in on mode; omit them for an unclassified on-mode call.

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
model, available input/cached-input/output usage, latency, known retries, policy
mode/use case/local attempts/reason and stop cause. The local report schema is
`source-scout-exploration-v2`. Cached input is part of input, not an additional total. Unknown
usage/cost is `null`; synthetic fallback records are not counted as requests.
`--trace-path` optionally writes an additional copy inside `.source_scout/`.
CLI exit status is nonzero for disabled, unavailable or incomplete exploration.
Details live locally, outside ordinary MCP output.

This policy governs additional Source Scout calls. It does not describe the
whole Codex workflow as offline. No verified cost-saving percentage is claimed.

## Preserved legacy catalog

The older discovery/assessment/bundle path remains frozen. Existing catalog,
snapshots and bundles are preserved; the reference schema is additive and no
destructive migration is performed. The explicit reuse MCP
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
