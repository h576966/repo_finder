# Product direction — 2026-09-06

Source Scout supports semi-autonomous Codex work across projects and worktrees.
It has two capabilities: Implementation References and Code Investigation.
Codex owns decisions, adaptations, changes and verification. No automatic search,
review, testing or model escalation policy belongs in the product.

References follow `references add -> find -> context`: explicitly selected
personal/curated repositories, immutable commit blobs, deterministic source-only
relevance gating, bounded context and explicit abstention. Metadata describes
the source; target fit is advisory. Temporary GitHub leads and file inspection
are explicit CLI operations. They never grow the collection automatically.

Investigation answers concrete unresolved relations after useful local methods.
Exact files/text use direct reads and rg; symbols/references use separate Serena
when available. Investigation preserves project policy, source safety, observation
validation, journals and a common request/deadline budget. Optional anchors avoid
repeating broad seed search. Observation counts are stop heuristics, not proof
that a contract or runtime relation has been established.

Normal MCP exposes exactly `investigate_code`, `find_implementation_references`
and `get_implementation_reference`. Two versioned plugin skills provide narrow
implicit triggers. There are no synonym tools on the normal surface. Source
roots and target project paths are call-specific; shared collection storage does
not imply shared project state.

The old application is removed, not relocated: no broad scout/qualify, capability
ontology/scoring, assessor/verdict, automatic refinement, outcome writer,
assessment-gated packaging or exclusive reuse eval. Historical tables are kept
for read/export, with additive schema compatibility only. Existing reports,
bundles and snapshots retain their identity and location. No real data GC runs.

Serena remains separately pinned at
`be609b625740846dc2750cc13291966cf1216b00`. Every session starts its own process
with `--project-from-cwd` and the actual session cwd; the fixed context omits activation, memory,
editing and shell tools. The Python profile also omits `find_implementations`.
No global mutable active project is shared between worktrees.

The earlier 385-test baseline and assessment experiment are historical evidence,
not current product acceptance. Current contract regressions and the observed
Codex trial are described in [development notes](development-notes.md) and
[integration evidence](integration-2026-09-06.md).
