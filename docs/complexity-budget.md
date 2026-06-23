# Complexity Budget

This document is the scope guardrail for Source Scout. Prefer small, local,
evidence-backed features over broad agent frameworks or generic search.

## Product Core

- Local-first catalog of reusable source candidates.
- Commit-pinned snapshots and reproducible local evidence.
- Deterministic file, path, dependency, freshness, and evidence validation.
- Task-specific reuse assessment for a candidate and a concrete user task.
- Small MCP surface that helps coding agents find, assess, bundle, and track
  reusable code.

## Allowed Near-Term Features

- Eval-backed scoring and shortlist tuning.
- Better deterministic evidence extraction and path/dependency signals.
- Gemma assessor calibration over validated evidence.
- Bounded FastContext refinement for missing or weak evidence.
- Standalone local exploration for the current repo or personal repos.
- Reuse outcome tracking tied to task signatures.
- Narrow new domains only after a golden eval suite exists.

## Deferred Features

- Broad framework, language, or repository-type coverage.
- Full dashboard or hosted UI product.
- Vector database or semantic index layer.
- Autonomous integration into target projects.
- Cloud/frontier model routing and provider abstraction.
- Multi-user accounts, auth, billing, or permissions.
- Full dependency, license, or legal compliance automation.

## Explicit Non-Goals

- Replacing GitHub search.
- Executing cloned repository code.
- Mutating user projects automatically.
- Building a public SaaS product.
- Ranking all repositories by generic quality.
- Adding external PR review workflows.
- Making license or legal reuse decisions.

## Default MCP Surface

Default MCP tools stay small:

- `find_reusable_code`
- `assess_reusable_code`
- `get_source_bundle`
- `record_reuse_outcome`
- `explore_local_code`

## Model Role Boundaries

- Deterministic code validates paths, line ranges, commit SHA, evidence hashes,
  scores, verdicts, and persistence.
- FastContext scouts for file and line evidence only.
- Gemma assesses validated evidence only; it does not write final scores.
- Codex reads cited source, edits code, and runs tests.
