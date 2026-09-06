# Complexity budget

This iteration is a bounded Codex sidecar, not a new development platform.

- Normal path: terminal checks + rg + optional direct Serena navigation.
- Check execution: existing Ruff/mypy/pytest, standard-library process control,
  Ruff JSON, pytest JUnit XML and bounded text summaries. No parser framework,
  generic MCP executor or intelligent test selection.
- Investigation: existing DeepSeek client and model, seven calls by default,
  at most twelve when explicitly selected, 240-second maximum inner deadline,
  zero SDK retries, finalization inside the same budget. No chained runs.
- Results: small MCP response; full local logs/trajectory in unique directories.
  Unknown metrics remain null. No pricing/provider abstraction.
- Source navigation: preserve rg, repo-map and Python AST. Serena is a separate
  optional process; no Source Scout LSP, persistent symbol index or new search API.
- Legacy: preserve catalog code/data/CLI, explicit reuse MCP profile, freeze
  feature work. Avoid unrelated refactors and directory/name migrations.
- Dependencies: no new Source Scout dependencies. Serena installation requires
  the host approval flow and a separate environment.
- Excluded: embeddings/vector databases, CocoIndex, RTK, SWE-Pruner, GitNexus,
  semantic indexing, local model servers, multi-provider routing, frontend,
  dashboard, reminder hooks and automatic integration.

Evaluate quality of finished changes before expanding scope. The manual
comparison in `source_scout_direction.md` is the next evidence gate; no automatic
paid evaluation or claimed savings percentage belongs in this implementation.
