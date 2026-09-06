# Product implementation, 2026-09-06

Baseline: clean `main`, `ec5266d78157b5c0a6c7dafc08b7cd34f25d7116`,
also verified with `git ls-remote origin refs/heads/main`.

1. Regressions and fixes: source-only evidence, canonical Git blobs, bounded
   reads/responses, supported target fit, process-safe catalog access, no GC.
2. Remove replaced executable reuse pipelines after extracting retained helpers.
   Preserve existing database tables, identities, snapshots, reports and bundles.
3. Name and simplify Code Investigation; retain its policy/budgets/validation and
   add validated start anchors. Expose three normal MCP operations.
4. Package two narrow skills and MCP configuration; inspect actual host discovery,
   migrate with reversible backups, and verify separate session-bound Serena.
5. Update current documentation, offline tests and CI. Run focused verification
   followed by `source-scout check` against final content.

No commit, push, paid live evaluation or cleanup of existing user data.

Dependency inventory before removal: reference-add imports pipeline only for an
unused repository card. References need repository/snapshot persistence, target
profile, path validation, GitHub transport and snapshot materialization. The
catalog facade imports scoring, assessment and asset workflows transitively.
CLI and server import the entire legacy application. Investigation's refinement
facade mixes local exploration with retired asset refinement. Extract retained
entry points and helpers; remove exclusive consumers and tests instead of moving
the old application into a legacy package. Keep additive historical table schemas
and a bounded read/export interface.
