# Project Instructions

Local-first Codex development for Source Scout. Be concise, avoid
over-engineering, and keep changes focused on the requested outcome.

## Current Product Priority

Optimize this loop:

```text
find_reusable_code -> assess_reusable_code -> get_source_bundle
```

Given a coding task, Source Scout should return one or a few evidence-backed
source bundles that make Codex faster and less wasteful.

## Workflow

1. Plan before non-trivial changes. A short written plan is enough when the
   direction is clear.
2. Implement in small, focused steps. Prefer existing project patterns over new
   abstractions.
3. Review locally after meaningful changes. Prioritize correctness, security,
   edge cases, and missing tests.
4. Verify before done. Use `source-scout check` unless explicitly impossible.

## Review Policy

This is a single-developer project. Reviews are local/manual through Codex or
direct code inspection only. Do not use CodeRabbit, `coderabbit`, or any
external/hosted PR review service.

## Model Role Boundaries

- Deterministic code validates paths, line ranges, commit SHA, evidence hashes,
  scores, verdicts, and persistence.
- FastContext scouts file and line evidence only.
- Gemma assesses validated evidence only; it does not write final scores.
- Codex reads cited source, edits code, and runs tests.

## FastContext Local Exploration

Standalone local exploration is available through the global `fastcontext-local`
Codex skill or:

```powershell
source-scout explore-local --project-path . --task "<task>"
```

Use it earlier for cold-start code comprehension, multi-file traces, impact
analysis, and cases where direct `rg` does not find the needed code. Prefer
`rg` first for exact symbols, exact files, commands, test names, config keys, or
quick-answer tasks. Treat FastContext output as read-only navigation.

The default local exploration budget is seven turns. After FastContext returns,
read only the top one or two cited ranges first, with
tight 30-80 line windows. Batch independent narrow reads when more than one
range is needed, do not repeat broad repository-wide searches for the same
question, and do not re-read regions already seen. If citations are sparse or
off-target, refine once with concrete symbols, subsystem names, or filenames
before falling back to manual `rg`.

## Engineering Constraints

- Do not add dependencies without discussion.
- Do not refactor unrelated code.
- Do not leave debug logs, TODO comments, or commented-out code.
- Do not execute arbitrary cloned repository code.
- Tie source analysis to exact commit SHAs.
- Keep generated data under `.source_scout/`.
- Keep all model outputs versioned by model, prompt, schema, and analyzer
  version.

## Local Checks

Use the local check wrapper as the default confidence gate:

```powershell
source-scout check
```

This runs:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
```

Use explicit eval commands only when tuning the relevant subsystem.
