---
name: investigate-code
description: Use Source Scout to trace a concrete source relation still unresolved after local navigation, such as indirect runtime flow, an inter-file contract, or ambiguous ownership. Ordinary text search, known-file edits and symbol lookup use direct tools.
---

Use Source Scout's `investigate_code` for the unresolved relation. Codex owns
reasoning, changes and verification; Serena remains a separate symbol navigator.

Supply the actual `source_root`, concrete `task` and `reason`, an applicable
`use_case`, and the `attempted_local_methods` already used (`rg`, `direct_read`,
or `serena`). Pass known files/symbols/ranges as optional `anchors`, for example
`[{"path":"src/dispatch.py","symbol":"dispatch","start_line":12,"end_line":35}]`.
Anchors are hints, not evidence. They never expand the permitted source root.

Discover the MCP operation by its accepted name. If MCP is absent, the equivalent
CLI is `source-scout investigate --source-root <root> --task <task> --reason <reason>
--use-case cross_file_contract --attempted-local-method rg --anchor src/dispatch.py:12-35`.
There is no mandatory navigation or tool chain before ordinary coding tasks.

Honor the project's off/selective/on policy. The user's standing authorization
covers sending the task and minimum relevant read-only source excerpts to DeepSeek
at `https://api.deepseek.com`; do not ask again or add routine permission messages.
Do not use remote investigation for material the user requires to remain offline.
An API key alone does not authorize the route.

Read useful cited source directly and assess it yourself. Observed range counts
are only stopping heuristics. For unavailable, incomplete, invalid or timed-out
results, report the missing relation and continue local navigation. Do not launch
smoke tests, switch providers, raise budgets, retry via CLI, or chain investigations
to bypass a limit. Preserve CLI process IDs until completion when execution yields.
