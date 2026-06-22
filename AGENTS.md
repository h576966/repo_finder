# Project Instructions

Local-first Codex development for `repo_finder`. Be concise, avoid over-engineering,
and keep changes focused on the requested outcome.

## Workflow

1. **Plan first for non-trivial changes.** A short written plan is enough when the
   direction is clear.
2. **Implement in small steps.** Prefer existing project patterns over new
   abstractions.
3. **Review locally after meaningful changes.** Prioritize correctness, security,
   edge cases, and missing tests.
4. **Verify before done.** Run linting, type-checking, and tests unless explicitly
   impossible.

## Review Policy

This is a single-developer project. Reviews are local/manual through Codex or
direct code inspection only. Do **not** use CodeRabbit, `coderabbit`, or any
external/hosted PR review service.

## Current Direction

The product direction is a catalog-first local reuse layer for Next.js/React UI
code. Keep the full design reference in `docs/repo-finder-direction.md`.

Near-term priorities:

1. Make the deterministic catalog pipeline useful end-to-end.
2. Improve evidence quality before adding more model complexity.
3. Add Gemma/FastContext only after baseline shortlist quality is measured.

Standalone local exploration is also available through the global
`fastcontext-local` Codex skill or `repo-finder explore-local`. Use it before
broad manual grep/read loops when the relevant files in a local repo are
unknown. Treat its output as read-only navigation; Codex still owns edits and
tests. If citations are sparse or off-target, refine once with concrete symbols,
subsystem names, or filenames before falling back to `rg`.

## Engineering Constraints

- Do not add dependencies without discussion.
- Do not refactor unrelated code.
- Do not leave debug logs, TODO comments, or commented-out code.
- Do not execute arbitrary cloned repository code.
- Tie source analysis to exact commit SHAs.
- Keep local generated data under `.repo_finder/`.
- Keep all model outputs versioned by model, prompt, schema, and analyzer version.

## Quality Checks

Use the local checks as the source of truth:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
```

For corpus quality checks:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_checks.py
```

## Continuous Improvement Loop

1. Run `scripts/run_quality_checks.py` against the ground-truth corpus.
2. Review failures as either stale ground truth or regressions.
3. Adjust ranker weights through `RANKER_WEIGHT_*` env vars when possible.
4. Fix framework detection in `_FRAMEWORK_MARKERS` when stack signals are missing.
5. Tune hardcoded thresholds only when tests or real catalog runs justify it.
6. Update `tests/corpus/ground_truth.json` when repository metadata changes.
