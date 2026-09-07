---
name: implementation-references
description: Use Source Scout when an implementation task would benefit from examples or patterns in explicitly selected personal or curated repositories. Searches commit-pinned source and can abstain; local project navigation uses direct tools.
---

Find ideas with `find_implementation_references(task, target_project_path=None)`.
Pass the current target project/worktree for optional advisory fit facts. The
reference collection root is configured separately through `SOURCE_SCOUT_HOME`;
it is neither the target project nor an investigation's `source_root`.

Read a useful match with `get_implementation_reference(reference_id, task,
target_project_path)`. Inspect its cited source before adapting it. Codex owns
relevance judgments, adaptation, edits and verification. Target fit is advisory;
unknown fit is not a compatibility guarantee. Current repository/license metadata
is separate from facts in the pinned commit.

An abstention means the searched collection supplied insufficient source evidence.
Do not auto-crawl, add repositories, call an assessor or request a bundle. If an
explicit GitHub search is appropriate to the user's task, use the bounded CLI
`source-scout references github-search --task <task>`. Results are temporary repo
leads. `references inspect --source <url> --commit <sha> --path <file>` verifies
selected source without adding it permanently. Add a repository only when selected
by the user: `references add --source <path-or-url> [--commit <sha>]`.

Equivalent local commands are `source-scout references find --task <task>
[--target-project-path <root>]` and `references context --reference-id <id>`.
For stale source, missing evidence, clipped presentation or transport failure,
report the actual limitation. Do not turn relative rank or metadata into proof.
References use no model. No remote operation is appropriate for offline-only data.

After assessing the result, record Codex's own feedback locally:
`source-scout feedback --report <usage.report_path> --outcome <outcome>
--observation <concrete benefit or missing evidence> [--evidence <source/test evidence>]`.
Use `helped`, `partly_helped`, `did_not_help`, or `unassessed`. Assess searches
and opened contexts, including abstentions; a search with no hits is not proof
that no relevant implementation exists. Mention related usage report paths when
find/context belong together. Do not ask the user for routine feedback or infer
usefulness from ranking or green tests alone. If you cannot assess it, leave it
unassessed. If `usage.error` is returned, mention the logging failure briefly.
Feedback records observations for later review; it does not change retrieval.
