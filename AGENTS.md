# Project Instructions

Be concise; avoid over-engineering. Source Scout is a personal, local Windows tool,
not a general-purpose package or public distribution. It provides Implementation References
and selective Code Investigation for Codex across projects/worktrees. Codex owns
reasoning, adaptation, edits, test selection and final assessment. Read
`docs/source_scout_direction.md` and `docs/complexity-budget.md`.

## Workflow

1. Write a short plan before non-trivial changes; protect uncommitted work.
2. Exact text/files use rg or direct reads. Symbols/references use Serena when
   useful and available. No mandatory tool chain.
3. Use `investigate_code` only for a concrete unresolved cross-file contract,
   indirect runtime flow, ambiguous ownership or architecture relation after
   relevant local navigation. Supply actual `source_root`, concrete `reason`,
   `use_case` and `attempted_local_methods`; pass known anchors when useful.
4. Use `find_implementation_references` for examples from explicitly selected
   personal/curated sources; read `get_implementation_reference` citations and
   make your own assessment. Abstention never triggers automatic GitHub crawling.
5. Review correctness, security, edge cases and missing tests locally.
   After using Source Scout, Codex records its assessment with `source-scout feedback`
   using `usage.report_path`, an outcome and a concrete observation. Do not ask the
   owner for routine feedback. Missing/insufficient evidence stays unassessed.
6. After project changes, run `source-scout check` in this trusted working copy. It runs this
   project's Ruff/mypy/offline pytest and stores logs/results under
   `.source_scout/checks/`. Reuse a successful report only when relevant contents,
   checks, dependencies and runtime are unchanged. Rerun after other edits, except
   a final `HANDOFF.md` results-only update: review that diff and disclose it as
   post-check documentation, not an exact-tree validation. Reports retain their
   recorded identity; never rewrite one to claim a match. Pure read-only reviews
   do not require a fresh check. Select other projects' checks yourself.

## Handoff and delivery

Read root `HANDOFF.md` for continuation context and verify the relevant current
source and Git state. After work changes project files, replace it with a concise
current snapshot before finishing, even without a commit. Preserve still-relevant
decisions and unknowns; Git carries older history. Include date/base revision,
changes and reasons, verification scope, limitations, work outside Git and one
bounded next suggestion. It is evidence, not permission to execute that suggestion.
Do not update it for a pure review or push-only task, or to embed its own commit
hash. Finalize it before staging when committing. No automatic commit or push.
When requested, direct push to `origin/main` is acceptable from the intended
`main` checkout; do not switch another worktree/branch just to follow that default.
ChatGPT setup and workflow acceptance scenarios: `docs/agent-workflow.md`.

## Authorization and limits

Standing Source Scout authorization: when the user asks Codex to work on a
repository, Source Scout/FastContext model operations may send the task and
minimum relevant read-only excerpts to DeepSeek at `https://api.deepseek.com`.
This persists across repositories/conversations until revoked. Do not ask again
or add routine permission disclaimers. Do not send material required to remain
offline. A key alone does not authorize a route; honor off/selective/on policy.

- Keep source root, target project and collection root distinct. Never install
  a global policy override or a global hardcoded target root.
- Keep Serena separate and bound to the actual project/worktree per process.
- No retries, provider switching, budget increases or chained investigators to
  bypass incomplete/error outcomes. Finalization shares the configured budget.
- Preserve data, IDs, snapshots, reports and bundles. No destructive migrations
  or real user-data cleanup. Retired pipelines and the reuse MCP profile stay retired.
- No arbitrary cloned code execution, hosted PR review services, new dependencies
  without discussion, new index/LSP/server/router, dashboard or reminder hooks.
- Optimize for the owner's actual local workflow; do not add distribution machinery,
  broad compatibility layers or speculative abstractions.
- Keep generated data under `.source_scout/`; never commit logs or keys.
- Do not run paid live evals, commit, push or create a PR unless requested.

Versioned plugin skills supersede the old global `fastcontext-local` and
`source-scout-reuse-flow`. Do not reinstall duplicate active copies. Their
controlled migration and backups are documented in `docs/integration-2026-09-06.md`.
