# Current handoff

Snapshot: 2026-09-07. Base: `1352c6f8b8d4f5748cee83d57ac077a5e1ba4503` on `main`.
This describes the accompanying workflow changes on top of that base, not live
commit, push or CI status. Verify the current revision and relevant diff before
continuing; suggestions below are not authorization to execute them.

## Changes and decisions

- Added one replaceable handoff and `docs/agent-workflow.md` with a ready-to-copy
  ChatGPT Ask Agent instruction and bounded acceptance scenarios. No hooks,
  automatic commits, extra skill or runtime changes.
- Project instructions now cover handoff before completion/commit, push-only and
  review exclusions, result reuse, and an explicit post-check handoff-only exception.
  Reports keep their original identity; no verification bypass was added to code.
- Source Scout skills now assess utility at a natural work-unit boundary; the
  investigation fallback ban on extra model probes does not ban local tests.
- The preceding base commit added Python 3.12+ policy, Codex usage feedback and
  evidence-based validation. Source Scout remains a personal local Windows tool.

## Verification and limits

- `source-scout check --format json` passed on 2026-09-07: Ruff, mypy and 279
  offline tests, zero failed/skipped. Local report:
  `.source_scout/checks/0e2a3460b3204d9d9a9f444a11bab29e/report.json` (pre-delivery rerun).
  The checkout was unchanged during checks. Only this handoff's results were
  updated afterward and reviewed separately; the report is not exact-tree proof
  of this final documentation. No runtime code changed in this work unit.
- All three modified skills passed the skill validator; the Source Scout plugin
  passed manifest validation. Installed skill hashes match repository sources.
- Walked through the eight scenarios in `docs/agent-workflow.md`; made the
  required-check failure gate explicit for push-only as well as commit-and-push.
  This was instruction review, not independent agent execution. No real commit,
  push or fresh ChatGPT conversation was performed to test those branches.
- Historical evidence in `docs/development-notes.md`: the 2026-09-06 paid navigation
  diagnostic completed 4/4 tasks with no invalid citations, but required-path
  scoring passed only 2/4. Pinned context missed `implementation_references.py`;
  deadline ownership missed `fastcontext.py`. Completion is not demonstrated utility.
- The owner subsequently requested commit and push of these workflow changes.
  Confirm actual delivery in Git/GitHub; no additional paid evals or retrieval/
  ranking changes are in scope. Fresh ChatGPT uptake remains untested.

## Outside Git and remaining work

- Global Codex instructions and `git-ship` were updated locally; they are not part
  of this repository's commits. Git Ship now separates push-only and checkpoint
  behavior, reviews the staged scope and makes validation limits explicit.
- Refreshed installed `source-scout@personal` to `0.3.0+codex.20260907164805`
  using the existing installer. Start a new Codex task for updated skills.
  MCP settings are byte-identical; global config differs only in line endings.
  Installer backups and global-instruction backups are under the owner's
  `.codex/backups/`, outside this repository. Existing data/logs were preserved.
- ChatGPT project settings cannot be changed through the available task tools.
  The instruction in `docs/agent-workflow.md` must be copied into that project;
  saving it locally does not activate it. Synced project mirrors were not edited.

## Next bounded suggestion

After the owner activates the ChatGPT instruction and shares/pushes this handoff,
try one fresh Ask conversation: request a small next Codex task with model/effort,
and verify that it distinguishes passing checks from the incomplete 2/4 navigation
result. Observe the listed delivery scenarios during ordinary work before adding
more policy. Do not start a broad refactor or heuristic adjustment from this alone.
