# Project Instructions

Source Scout is a small Codex sidecar for reliable local check results and
optional, bounded read-only code exploration. Codex owns reasoning, edits,
test selection and final assessment. Do not sacrifice finished-change quality
for fewer tokens. See `docs/source_scout_direction.md` and
`docs/complexity-budget.md`.

## Workflow

1. Write a short plan before non-trivial changes; protect uncommitted work.
2. Exact text/file: use `rg` or direct reads. Symbols/references: use Serena
   when available. No mandatory tool cycle or reminder hooks.
3. Use the DeepSeek investigator only for unresolved cross-file contracts,
   indirect runtime flow, ambiguous ownership or concrete architecture relations
   after local navigation. Project default is selective: supply `use_case`, a
   concrete `reason` and `attempted_local_methods` (rg, direct_read or serena).
   Serena is optional; no mandatory call before every task. An API key alone
   does not authorize a route. Standing authorization applies; do not ask again.
4. Treat citations as navigation. Codex reads the source and makes changes.
5. Review locally for correctness, security, edge cases and missing tests.
6. Run `source-scout check` before done. It runs Ruff, mypy and pytest in the
   trusted working copy and saves logs/results under `.source_scout/checks/`.
   A report verifies only its recorded working-copy identity; rerun after edits.

## Constraints

- Keep changes focused. Do not add dependencies without discussion.
- No external/hosted PR review services, including CodeRabbit.
- Do not execute arbitrary cloned repository code.
- Keep generated data under `.source_scout/`; never commit logs or keys.
- Keep model output versioned by model, prompt, schema and analyzer.
- The investigator only scouts permitted source; it never edits, assesses
  candidates, selects tests or summarizes test logs in the default flow.
- Use at most the configured call/deadline budget, including finalization.
  Report missing context; do not chain investigators to bypass budgets.
- Keep repo-map, rg and Python AST as support/fallback. No new index, LSP,
  provider router, search product or dashboard.
- Catalog feature development is frozen. Preserve CLI compatibility and data;
  catalog MCP tools require explicit `serve-mcp --profile reuse`.
- Do not run paid live evals, commit, push or create a PR unless requested.

The older global `fastcontext-local` skill must not override these routing and
selective-use rules. Its migration is documented in `docs/source_scout_direction.md`.
