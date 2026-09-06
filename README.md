# Source Scout

Source Scout gives Codex implementation examples from explicitly selected Git
repositories and bounded investigation of unresolved source relations. Codex
owns reasoning, adaptation, edits and verification. Use ordinary text tools and
Serena directly when they answer the question.

## Install

Python 3.11+ and Git are required. From this trusted checkout:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[all,dev]"
.venv\Scripts\python.exe scripts/install_codex.py
```

On Linux use `.venv/bin/python`. The base wheel has no runtime dependencies;
`references`, `investigation`, `mcp`, `all` and `dev` extras select existing
dependencies. `check --help` and check dispatch do not import model, catalog or
MCP packages. Running checks needs the dev tools; the complete product test suite
also needs the all extra.

The installer uses Codex's plugin-creator helpers and personal marketplace. It
binds the plugin to this Python/source installation, preserves the existing
collection path, backs up settings and retires only the two old Source Scout
skills. It sets no global source root or exploration policy. Start a new Codex
thread after installation. See [migration and observed integration](docs/integration-2026-09-06.md).

## Three operations, two skills

| MCP operation | Purpose | Important inputs |
|---|---|---|
| `investigate_code` | A concrete relation unresolved after local navigation | `source_root`, `task`, `reason`, `use_case`, `attempted_local_methods`, optional `anchors` |
| `find_implementation_references` | Deterministic lookup in the selected collection | `task`, optional `target_project_path` |
| `get_implementation_reference` | Canonical source context and provenance | `reference_id`, optional `task`, `target_project_path` |

The plugin contains `investigate-code` and `implementation-references`, with
narrow implicit triggers. There is no required tool chain. `serve-mcp` exposes
all three; `--profile investigator` exposes only investigation and
`--profile references` only reference lookup/context. `sidecar` is a transitional
CLI alias for investigator. The retired `reuse` profile cannot restore old tools.

Serena is configured separately per project/session. The pinned configuration
uses `--project-from-cwd`, so each process follows that session's worktree.
Other Git projects need their own Serena connection and the bounded read-only
project/context configuration; the Source Scout plugin does not register a
global Serena target. See the integration document for the observed host test.

## Implementation References

Choose each source explicitly. Committed source is read; repository code, tests,
filters and hooks are never executed.

```powershell
source-scout references add --source C:/code/my-library --kind personal
source-scout references add --source https://github.com/owner/repo --kind curated --commit <full-sha>
source-scout references find --task "bounded decorrelated jitter retries" --target-project-path C:/code/app
source-scout references context --reference-id <id> --task "bounded jitter retries"
```

`SOURCE_SCOUT_HOME` is the collection root, separate from the source being read
and the target receiving adaptations. It defaults to the current directory's
`.source_scout`. Use the same explicit collection for several worktrees. Database
connections and transactions close after each operation, with a five-second
interprocess lock wait. Fetch/index work holds no database lock. A failed catalog
write rolls back atomically; an already generated snapshot can remain for reuse.

Retrieval gates on file paths, identifiers and source text. Repository metadata,
manifest names, personal priority and advisory target fit cannot create a match.
Abstention does not trigger GitHub crawling. Python and Node target-fit signals
are advisory; unsupported ecosystems are unknown. `.cjs` and `.cts` are indexed.

Git blobs define whole-file hashes, including when attributes request CRLF.
Snippets use one-based whole-line ranges and SHA256 over the normalized excerpt:
UTF-8 replacement decoding, CRLF to LF, no trailing newline, then numbered display
lines. Permalinks identify the exact commit/path/range. Historical materialized
hashes remain separate. Changed snapshots fail closed.

Normal reference JSON is capped at 64,000 serialized bytes, including metadata
and manifests. Source reads are capped at 240,000 bytes per blob, snapshot
materialization at 30 MB/6,000 entries. Catalog retrieval reads at most 2,000
indexed files and 8 MB of row presentation. Whole lines that cannot fit are
omitted, never given misleading partial-line citations. `truncated` describes
presentation; `missing_evidence` describes absent source or provenance. Neither
means an investigation succeeded. See [precise budgets](docs/complexity-budget.md).

Explicit temporary GitHub fallback:

```powershell
source-scout references github-search --task "bounded jitter retries"
source-scout references inspect --source https://github.com/owner/repo --commit <full-sha> --path src/retry.py
```

Search returns repository leads, not verified implementations. Inspection checks
the commit/tree/blob chain and hashes without adding to the collection. Neither
persists source. `GITHUB_TOKEN` authenticates GitHub REST. Private Git fetch uses
the user's configured Git credentials separately; no token is put in a remote,
command argument or stored config. Current repository/license observations are
labelled separately from facts in the pinned commit. A mock HTTP authentication
test does not establish private Git access.

## Code Investigation

Set policy per source root in `.source-scout.toml`:

```toml
[remote_exploration]
mode = "selective"
```

Missing/invalid policy fails closed to `off`. Explicit process environment
`SOURCE_SCOUT_REMOTE_EXPLORATION` overrides project policy, so never install a
global selective override. Standing user authorization for minimum read-only
source to DeepSeek persists; a key alone does not authorize a route.

```powershell
source-scout investigate --source-root C:/code/app --task "Trace callback ownership" --reason "Local references do not resolve dynamic registration" --use-case indirect_runtime_flow --attempted-local-method rg --anchor src/dispatch.py:12-35
```

Selective use cases are `cross_file_contract`, `indirect_runtime_flow`,
`ambiguous_ownership`, and `architecture_trace`. Known anchors skip broad seed
search and are validated within the same root and policy. They are hints; source
must still be read before citation. Seven requests by default, at most twelve,
share finalization and a maximum 240-second deadline. SDK retries are zero.
Disabled, incomplete, stale, failed and timed-out outcomes remain explicit; do
not retry, switch providers, raise budgets or chain investigations automatically.
Full journals stay under the source root's `.source_scout/explorations/`.

## Development and migration

`source-scout check` runs this project's Ruff, mypy and offline pytest contracts
in its trusted working copy and saves identity-bound results under
`.source_scout/checks/`. It is not a general test selector for other projects.
See [development notes](docs/development-notes.md) for focused checks and CI.

The scout/qualify, capability scoring, model profiling/assessment, automatic
refinement, outcome and bundle-building pipelines are removed. Old commands
return migration errors without starting them. No GC or assessment-gated
packaging remains. Existing DuckDB tables, IDs, snapshots, reports and bundles
are preserved; no user-data cleanup is part of installation.

`references export-data --table <table> --limit 100 --offset 0` reads historical
records unchanged for explicit administration (pagination is by rows, not a
normal bounded reference response). Existing reports/bundles remain readable as
files. Old reference IDs still open; their recorded materialized hash is exposed
as `historical_content_sha256`, while current source uses Git-blob hashes.
Old indexes are not silently reinterpreted: re-add an explicitly selected source
to build v2 identities alongside its history.
