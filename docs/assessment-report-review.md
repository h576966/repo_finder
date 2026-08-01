# Assessment Report Review

Use this guide when reading `source-scout assess` output or
`source-scout eval-assess` reports.

## Key Fields

- `assessment_id`: Stable identifier required by `get_source_bundle`. Do not
  reconstruct bundle inputs from candidate and task fields.
- `target_profile_fingerprint`: Canonical fingerprint of the optional read-only
  target-project profile. Reusing the same target facts produces the same
  profile-aware task signature and assessment cache key.
- `recommended_verdict`: Gemma's recommendation from the validated evidence.
  Treat it as model judgment, not the final decision.
- `final_verdict`: Source Scout's deterministic verdict after applying evidence
  coverage, score, confidence, and blocker rules. This is the field to use for
  workflow decisions.
- `reuse_score`: Deterministic 0-1 score for task fit, extractability,
  dependency fit, low coupling risk, low maintenance risk, and evidence
  coverage. Higher is better, but it should be read with `evidence_coverage`.
- `evidence_coverage`: Share of assessed requirements backed by validated
  evidence paths. Low coverage means the score is not well grounded yet.
- `fastcontext_status`: Whether focused evidence refinement was used.
  `not_requested` is normal when deterministic evidence is enough.
- `license_status`: Passive GitHub metadata only. It is useful context, but it
  does not affect scoring or `final_verdict`.

## Verdicts

- `select`: Strong candidate for the task. It may create a normal bundle only
  when required source and local-import closure validation succeeds.
- `inspect`: Potentially useful, but needs manual review or more evidence. It may
  create a clearly marked inspection bundle with explicit warnings.
- `reject`: Evidence shows poor fit, hard blockers, or too much coupling.
- `insufficient_evidence`: Do not judge the candidate yet; refresh or refine
  evidence before spending integration time.

`reject` and `insufficient_evidence` cannot create bundles. A stale
schema/analyzer, commit/snapshot mismatch, or missing validated adaptation path
also requires reassessment before bundling.

## Fast Review Loop

1. Start with `final_verdict`, `reuse_score`, and `evidence_coverage`.
2. Compare `recommended_verdict` and `final_verdict`; differences usually mean
   deterministic evidence or blocker rules changed the model recommendation.
3. Read the top `reasons` and their evidence paths.
4. If `evidence_coverage` is low or `missing_evidence` is important, rerun with
   `--fastcontext-policy auto` or `always`.
5. For `select` or `inspect`, call `get_source_bundle(assessment_id)` and read
   required files before optional files.
6. Ignore license as a scoring signal. Review it manually only when you plan to
   reuse code outside private experimentation.

## Calibration Signals

- Good eval runs should have high `verdict_match_rate`, low
  `insufficient_evidence_rate`, and few unknown evidence ID repairs.
- `fastcontext_error_count` should not force failure when deterministic evidence
  is already enough.
- If many strong candidates land as `inspect`, check evidence coverage and hard
  blockers before tuning model prompts.
