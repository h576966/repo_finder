# Observed integration — 2026-09-06

The baseline checkout was clean and matched pushed main
`ec5266d78157b5c0a6c7dafc08b7cd34f25d7116`. No product commit/push, paid live eval or
real catalog cleanup was performed. All test references use generated isolated
collections. The configured real collection remains `C:/AI/SourceScout`.

## Host discovery and migration

CLI 0.153.0 was interrogated through local app-server JSON RPC without a model
turn. Before migration it discovered one enabled `fastcontext-local` and one
`source-scout-reuse-flow` in `~/.codex/skills`. After migration it discovered exactly
one `source-scout:investigate-code` and one `source-scout:implementation-references`
in the installed `source-scout@personal` plugin, with no skill discovery errors.
The same result was observed with a second project cwd.

An ephemeral host thread, without `turn/start`, confirmed connected plugin MCP
with exactly `investigate_code`, `find_implementation_references`, and
`get_implementation_reference`. Separate Serena connected with six tools:
`initial_instructions`, `get_current_config`, `get_symbols_overview`, `find_symbol`,
`find_referencing_symbols`, `find_declaration`. The unsupported Python operation
and project-switch/edit/shell/memory operations are absent from its active list.

The installer backed up global config and the old skill directories outside active
skill roots at:
`C:/Users/Nikla/.codex/backups/source-scout-20260906T145310582384Z/`.
It preserves other settings/skills and the original collection. Repo-specific
duplicate Source Scout MCP entries were removed. No global selective environment
override was added. Plugin source lives in `~/plugins/source-scout`; versioned
source of truth is this repository's `plugins/source-scout`.

Rollback is explicit: `codex plugin remove source-scout@personal`, restore the two
directories listed in `migration.json` to their original paths, and restore the
Source Scout section from backed-up `config.toml`. Restore only those settings if
other settings have since changed. This does not touch catalog data. For an
update, rerun `scripts/install_codex.py` from the intended venv and start a new
thread. The installer applies a single timestamp cachebuster in the installed
copy and leaves the versioned plugin's product version unchanged.

## Source roots and observed tool choice

Standard integration tests start two real Source Scout stdio processes in two
Git worktrees. They share one collection, return the same reference ID, and honor
each call's source root and project off policy with no API key. Separate pinned
Serena stdio processes read `flow.py` in two worktrees: `handler` returned 1 and
2 respectively, with `dispatch` correctly identified as its caller. Neither
connection offers `activate_project`; no mutable active root is shared.

The final repo configuration uses `--project-from-cwd`, with no fixed `cwd` or
target root. An actual Codex host session bound to the second worktree returned
the body containing `return 2`. A distinct Git project does not inherit this
repo's `.codex/config.toml`; the probe supplies the same Serena table as a
session-only override. For another project, configure that separate Serena
connection there and copy `config/serena-project.yml` to `.serena/project.yml`.
Do not register a global target root. Generated proof is in
`.source_scout/integration/codex-worktree-bound.json` and `serena-cwd.json`.

An independent Codex evaluator read both skills and performed six requests:

| Request | Observed choice/outcome |
|---|---|
| Exact arithmetic edit | Direct read/edit; executed 20%, 0%, 100% checks passed |
| Python definition/callers | Serena find_symbol then find_referencing_symbols |
| Unresolved dynamic plugin relation | rg/read first; missing runtime manifest identified; anchored investigation route selected |
| Reference example | Explicit local add, find and context; 82 indexed references and complete 17-line selected retry snippet |
| Absent GPU pattern | Abstained on the populated collection without GitHub expansion |
| Unavailable investigator | Observed off-policy response; continued local reasoning, no retry/provider/budget change |

The first reference add exposed a Windows GitDB mapping lifetime bug. Explicit
pack-map close fixed the real reproducer; the evaluator reran add/find/context
and populated abstention successfully. Source omissions, manifest clipping and
unknown license stayed explicit. No skill-routing defect was demonstrated.

Remote successful investigation, remote timeout behavior in a live model turn,
real private Git fetch and Linux execution remain unverified. Mocked transport
regressions exercise deadlines/error/budget behavior; the observed trial is a
small directional check, not a skill-selection benchmark.

Generated evidence stays uncommitted: `.source_scout/integration/codex-before.json`,
`codex-after.json`, `codex-mcp.json`, `serena-after.json`, and
`.source_scout/skill-forward-test/report.md` plus its logs. Final Ruff/mypy/pytest
results and exact working-copy identity are in `.source_scout/checks/`.
