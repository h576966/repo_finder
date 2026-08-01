# Source Scout Direction

Source Scout should optimize one job:

> Given a coding task, return one or a few evidence-backed source bundles that
> make Codex faster and less wasteful.

This document is a short product orientation. Scope boundaries live in
[`complexity-budget.md`](complexity-budget.md).

## Product Shape

Source Scout is a local source reuse assistant for coding agents. It
is focused on your working stack: TypeScript, JavaScript, Python, AI/local-AI
harnesses, data tooling, Next.js, Node, and React. It is not a generic GitHub
search replacement, repo ranking site, SaaS product, or autonomous integration
system.

The useful workflow is:

```text
coding task
  -> find_reusable_code(task, optional target project)
  -> assess_reusable_code(candidate, task, same target project)
  -> get_source_bundle(assessment_id)
  -> Codex reads cited source, edits, and tests
```

The output should be small and actionable: exact files, line evidence, commit
SHA, target-fit facts, dependencies, adaptation notes, and an assessment-linked
bundle. Returning no candidate is correct when relevance evidence is weak.

## Architecture Direction

- Deterministic catalog retrieval applies a versioned, precision-first relevance
  threshold before target compatibility. It may abstain.
- Read-only target profiling detects languages, source/test roots, manifests,
  dependencies, framework/test signals, Node module format, and basic
  TypeScript configuration. Only canonical facts and a fingerprint are stored.
- Deterministic code remains responsible for scores, verdicts, bounded file
  access, path safety, line-range and exact-SHA validation, dependency closure,
  hashing, persistence, traces, manifests, and eval metrics.
- Gemma interprets validated assessment evidence. FastContext scouts file and
  line evidence only. Neither writes final scores or bypasses bundle gates.
- Model reranking, outcome-based ranking, and full RLM orchestration stay
  deferred until deterministic retrieval and bundle evals establish a baseline.

## Current Product Path

- Build and maintain a local catalog of recent, public, commit-pinned source
  snapshots from the opinionated `personal-code` discovery domain.
- Extract deterministic evidence from paths, manifests, dependencies, and source
  files without executing repository code.
- Use Gemma and FastContext as local model roles inside that architecture:
  FastContext finds file and line evidence, while Gemma assesses validated
  evidence for a specific task.
- Recompute deterministic target profiles during find and assessment, and tie
  profile-aware work to the canonical fingerprint.
- Create bundles only from current `select` or `inspect` assessments. Seed them
  from validated adaptation paths, follow bounded local imports, and publish
  atomically under the assessment ID.
- Track reuse outcomes against the task signature; with a target profile, the
  signature includes its fingerprint.

## Model Roles

- Deterministic code validates, bounds, hashes, gates, fingerprints, and
  persists.
- FastContext scouts evidence as a read-only specialist.
- Gemma interprets validated evidence for task-specific assessment.
- Codex reads the cited source and owns edits/tests.

These boundaries keep Source Scout practical as a personal developer tool while
leaving room for separately evaluated reasoning experiments later.

## Practical Priorities

Near-term work should make the core loop more useful without expanding into a
hosted or autonomous integration product:

- Calibrated no-match and positive-retrieval quality.
- Better target-project fit without executing or mutating the target.
- Assessment-driven dependency-complete bundles with fewer irrelevant files.
- Better assessment calibration from golden evals.
- Lower token/time waste for Codex through local exploration.
- Simpler code and tests around the active product path.

No accidental mutation of user projects and no execution of cloned repository
code remain hard boundaries. If an idea does not improve project understanding,
candidate comparison, bundle usefulness, or eval learning speed, keep it out of
the main path.
