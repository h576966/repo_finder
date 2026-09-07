# Bounded Source Scout validation — 2026-09-07

Base: `6a85997fe934d42c694bc2297e9b26d93d161741`, clean local `main` at entry,
matching `origin/main`. [Windows CI](https://github.com/h576966/source_scout/actions/runs/34145850752)
passed on Python 3.12 and 3.14 for that SHA. Only this report and `HANDOFF.md`
change; they are local, uncommitted documentation, not pushed main.

## Conclusion

Keep the current implementation. All twelve authorized investigations ran: eleven
completed, one explicitly incomplete, nine passed the existing structural oracle.
Codex's separate source-based assessment found three sufficient explanations and
nine partial ones under the frozen criteria. These counts describe this small
experiment, not a general quality or productivity rate. No oracle change is needed.

The recurring gap is deadline enforcement: all three answers omit `fastcontext.py`.
File hits also hide smaller missing links in catalog and snippet answers. Actual
utility is locating relevant source and explaining parts of a relation; replacement
of ordinary Codex navigation is not demonstrated.

## Locked protocol and evidence

The user directly approved twelve paid runs after two earlier approval rejections
before process creation. Those rejected launches consumed no investigations.
Three fixed rounds ran in order: `reference_evidence`, `shared_catalog`,
`pinned_context`, `policy_deadline`. No failed/incomplete slot was replaced.

The existing `eval-navigation --suite source-scout` runner used DeepSeek v4 flash,
temperature 0, reasoning none, 3,000 output tokens/request, seven total calls
including finalization, 240-second task/inner deadline, 120-second request timeout,
zero SDK retries, selective policy, no anchors and no availability probes.
Python was 3.12.10; exact dependencies/settings are in the lock.

Under `.source_scout/validation-20260907/`:

- `protocol.json` freezes tasks/order and source-based criteria before any run.
- `lock-approved.json` freezes the actual pre-run tree, suite and environment;
  `lock-after-runs.json` is identical, including content hash
  `9994afc361ab8e451c37b75ad93635240a5b33ef4a5e5777fe9cf85bc4a6a0bf`.
  The earlier clean-tree `lock.json` remains separate and unchanged.
- `repetition-{1,2,3}.json` and corresponding stdout/stderr logs are originals.
  `run-audit.json` joins full journal paths/hashes, observations, usage and metrics.
- Twelve usage results under `usage-home/usage/` have `kind=evaluation` and the
  same source-code digest. Codex's assessments and append-only feedback are local.
  Full investigation journals remain under `.source_scout/explorations/`.
- `execution-status.json` preserves the old approval block; `completion-status.json`
  records completion. One-off offline audit/aggregation scripts make no model calls.

Method limitation: the locked tree included the pre-live report and handoff.
Offline seed reconstruction on that unchanged tree includes HANDOFF assessment
lines for `reference_evidence` and `policy_deadline`. Journals do not capture the
entire initial request: this is reconstructed input evidence, not a saved prompt.
It exposes potential assessment leakage; this is not a clean blind eval. No model
tool read the report/handoff; all observed reads were source files. No additional
runs were made to remove this limitation.

## Historical evidence and oracle review

Original reports exist under `.source_scout/local_explore_eval_runs/local-explore-source-scout/`:

- `20260906_184739_evidence-20260906.json`: four pre-model route errors,
  `Selective exploration requires an approved use_case.` No model accounting.
- `20260906_184949_evidence-20260906-fixed.json`: four completed investigations,
  nine model requests, 18.5719 seconds; all four full journals exist.

The latter says four task passes but overall failure: four extra citations were
outside its then-empty acceptable lists. Commit `1352c6f` made scoring require all
required paths and added supporting acceptable paths. A separate current-source
re-score gives **2/4**, zero unexpected/invalid ranges. Originals were not rewritten.
Full historical checkout identity is unknown; embedded source and actual answers,
not an inferred historical revision, were reviewed.

| Historical task | Current structural score | Codex assessment |
|---|---|---|
| reference_evidence | Pass | Sufficient source gate before candidate fit |
| shared_catalog | Pass | Partial: caller range ends before catalog write scope |
| pinned_context | Fail | Partial: blob utility, not reference-to-snippet integration |
| policy_deadline | Fail | Partial: policy/configuration and accounting, not enforcement |

For pinned_context, `snapshotter.py:40-159` and `github_client.py:155-192` explain
blob utilities but omit the actual getter in `implementation_references.py`.
For policy_deadline, `exploration_policy.py:1-90` and `exploration_trace.py:1-32`
do not establish the off return, shared timer or finalization budget owned by
`fastcontext.py`. These are substantive missing links, not an overly narrow oracle.
Historical usage: 81,938 input / 1,363 output tokens; cached input 19,456 is already
part of input. Cost is null. All four claimed `missing_context=false`.

## Twelve new results

S = sufficient, P = partial under the pre-run criteria, assessed by Codex from full
notes and source. Structural pass is separate. Cached tokens are a subset of input.

| Round | Task | Structural | Assessment | Seconds | Requests | Input / output | Cached input |
|---|---|---|---|---:|---:|---:|---:|
| 1 | reference_evidence | Pass | S | 4.2173 | 2 | 18,706 / 279 | 2,432 |
| 1 | shared_catalog | Pass | P | 6.1253 | 3 | 29,986 / 477 | 13,184 |
| 1 | pinned_context | Pass | P | 5.0084 | 2 | 18,456 / 310 | 2,432 |
| 1 | policy_deadline | Fail | P | 4.2609 | 2 | 16,870 / 230 | 2,432 |
| 2 | reference_evidence | Pass | S | 4.5465 | 2 | 18,248 / 270 | 2,432 |
| 2 | shared_catalog | Pass | P | 6.5838 | 3 | 29,731 / 572 | 13,184 |
| 2 | pinned_context | Pass | P | 4.4850 | 2 | 18,552 / 332 | 2,432 |
| 2 | policy_deadline | Fail | P, incomplete | 3.7607 | 2 | 16,852 / 243 | 2,432 |
| 3 | reference_evidence | Pass | S | 4.5461 | 2 | 18,215 / 276 | 2,432 |
| 3 | shared_catalog | Pass | P | 6.1075 | 3 | 29,430 / 540 | 12,928 |
| 3 | pinned_context | Pass | P | 3.9830 | 2 | 18,376 / 303 | 2,432 |
| 3 | policy_deadline | Fail | P | 4.5528 | 2 | 16,968 / 208 | 2,432 |

Total measured task time **58.1773 seconds**, **27 requests**, **250,390 input /
4,040 output tokens**, **61,184 cached input**. This excludes Codex review and
between-round time. Usage is complete; monetary cost is null, not zero. No savings
were estimated. No model errors/timeouts occurred; the incomplete result admitted
missing evidence. Each suite exits unsuccessfully because not every task passes;
progress text wrapped as PowerShell NativeCommandError is stderr formatting, not
a model failure.

### Explanation and citation assessment

- **reference_evidence (3/3 sufficient):** same useful gate at
  `implementation_references.py:273-392`: matched terms, coverage and BM25 precede
  candidate fit/priority, with abstention. `models.py:13-82` declares fields, not
  execution ordering. Early profile construction at 283 is distinct from candidate
  fit at 308. The core is supported despite imprecise attribution to the dataclass.
- **shared_catalog (3/3 partial):** all locate the caller, `catalog_core.py:40-99`
  (round 3 through 119) and `file_lock.py:1-44`; transaction cleanup is correctly
  described. Rounds 1–2 cite caller 100-259 but omit explicit fetch/index-before-lock
  sequencing; round 2's wording that the lock covers the whole add is too broad.
  Round 3 states the boundary but cites only 100-219, ending at the first write.
  All omit nested reuse at `catalog_core.py:59-64`, important because upsert helpers
  enter `get_connection()` again. Useful evidence, incomplete lifetime trace.
- **pinned_context (3/3 partial):** unlike the old run, all connect the real getter
  (360-419, or 360-399 in round 3) to `snapshotter.read_blob` (103-119) and correctly
  explain why no checkout filters run. All stop short of `_source_lines`/`_snippets`
  at 908-959: UTF-8/CRLF normalization, whole-line bounds, separate excerpt hash
  and permalink. Round 2 conflates file bytes with snippet bytes. Digest
  revalidation also has a legacy exception. Useful core byte retrieval, but not
  the full frozen snippet/provenance criterion; all say no missing context.
- **policy_deadline (3/3 partial):** same policy 1-90/accounting 1-32 citations,
  no `fastcontext.py`. Enforcement is at 145-148 (off return), 174-204 (shared
  remaining timer), 231-234 (bounded timeout), 350-368 and 487-514 (same call loop
  and finalization). Round 2 alone acknowledges the unread owner and returns
  incomplete; rounds 1 and 3 overstate completeness. Environment precedence is
  visible in cited policy but not explained.

All final ranges are valid, observed and current; the audit compared observed
numbered lines and whole-file hashes to local source. Zero structurally invalid,
unexpected or unsupported ranges does **not** mean every claim is entailed. All
responses are untruncated. Missing semantic links are not a presentation-size loss.

All twelve journals request finalization after the first source-read turn with
`enough_primary_source_ranges`. This count heuristic is not a semantic completeness
check. Each catalog run then uses one existing, within-budget priority-citation
correction: the first final answer omitted the observed caller, the corrected
answer cites it without new source reads. These three extra requests are included
in 27; no investigator was relaunched and no retry policy was added. Early
finalization and omitted links recur together, but causation or a better stopping
rule is not established.

## Implementation References offline coverage

`tests/test_reference_quality.py` copies an isolated, committed seven-source-file
corpus and runs `evals/golden/references_v1.json`: four top-hit/text-fragment checks
(launcher, token rotation, retry delay, event dispatch) and two abstentions (GPU,
Kubernetes). Small related distractors and identifier-like queries protect a fixed
retrieval/context contract. The test does not execute algorithms, assess adaptation,
test target fit, or cover diverse natural queries/large collections. The retry
case checks a name fragment, not correctness of a jitter algorithm. Other offline
tests protect safety, hashes, bounds, locking and profiles separately.

No normal Codex comparison was started. The navigation evaluator's manual-search
file-count proxy is not that workflow and cannot establish time/token savings.
Historical/new results are not a controlled before/after quality comparison.

## Validation and next bounded actions

The final `source-scout check` result/path is in `HANDOFF.md`; its final results-only
edit is reviewed separately under the project's exception. Product code, tests,
prompts, ranking, model and golden files are unchanged. No commit/push, real-data
cleanup, dependency or benchmark framework was introduced. The skill's source-review
and local-feedback requirements informed assessment, not product edits.

1. Keep current implementation. Use the saved deadline cases for a bounded local
   review of completeness/uncertainty reporting before proposing a stopping change;
   do not add a file-name heuristic or weaken the oracle.
2. Before any later paid evaluation, keep assessment notes outside the inspected
   source tree and freeze that input. Do not rerun these twelve merely for a pass.

No new regression test is proposed yet: a generic desired behavior/fix must first
be specified. Existing required-path scoring already detects the repeated deadline
omission; this pass does not justify architectural cleanup or broader refactoring.
