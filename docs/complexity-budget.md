# Complexity Budget

This document is the scope guardrail for Source Scout. The budget stays local,
bounded, and evidence-backed rather than hosted, autonomous, or generic search.

## Product Core

- Local-first catalog of reusable source candidates.
- Commit-pinned snapshots and reproducible local evidence.
- Deterministic file, path, dependency, freshness, and evidence validation.
- Precision-first retrieval with explicit no-match abstention.
- Read-only deterministic target-project profiling and compatibility signals.
- Task-specific reuse assessment for a candidate and a concrete user task.
- Assessment-gated, dependency-aware, atomically published source bundles.
- Small MCP surface that helps coding agents find, assess, bundle, and track
  reusable code.

## Allowed Near-Term Features

- Eval-backed scoring and shortlist tuning.
- Positive/no-match retrieval and bundle-quality evals.
- Read-only target-project fit signals.
- Dependency-free BM25 role cards only while they pass regression, ablation,
  and context-efficiency gates.
- Optional structural parsing only when fixtures demonstrate a real miss and
  installation/precision/recall gates pass.
- Better deterministic evidence extraction and path/dependency signals.
- Assessment-model calibration over validated evidence.
- Bounded FastContext refinement for missing or weak evidence.
- Standalone local exploration for the current repo or personal repos.
- Reuse outcome tracking tied to task signatures.
- Narrow new domains only after a golden eval suite exists.

## Deferred Features

- Broad framework, language, or repository-type coverage.
- Full dashboard or hosted UI product.
- Vector or graph database or semantic index layer.
- Autonomous integration into target projects.
- Full open-ended RLM controller without eval coverage and hard tool bounds.
- Model reranking, outcome-based ranking, and full RLM orchestration before the
  deterministic/BM25 baseline is established.
- Online self-adjusting ranking.
- Multi-provider routing and automatic provider failover.
- Multi-user accounts, auth, billing, or permissions.
- Full dependency, license, or legal compliance automation.

## Explicit Non-Goals

- Replacing GitHub search.
- Executing cloned repository code.
- Mutating user projects automatically.
- Letting RLM tools write files, run shell commands, or alter target projects.
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
  scores, verdicts, bounded file access, manifests, traces, eval metrics, and
  persistence.
- Catalog search owns deterministic relevance and target-fit ranking and may
  abstain below its calibrated threshold.
- The exploration role scouts for file and line evidence only.
- The assessment role assesses validated evidence only; it does not write final scores.
- Codex reads cited source, edits code, and runs tests.
