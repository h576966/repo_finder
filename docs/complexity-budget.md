# Complexity budget — 2026-09-06

Keep two capabilities and three normal MCP operations. No new runtime dependency,
DB server, provider router, LSP/index server, ontology, dashboard or reminder hook.

| Area | Local bound or decision |
|---|---|
| Reference presentation | 64,000 serialized ASCII-JSON bytes; at most 3 matches |
| Metadata | 8,000 presentation bytes, depth 6, 20 entries per collection, strings 500 characters |
| Snapshot | 240,000 bytes/blob, 30 MB materialized, 6,000 tree entries; fetched Git pack size is not bounded by these materialization limits |
| Reference index | 1,500 files, 30 MB total, 12,000 terms/file, source-only term extraction |
| Catalog retrieval | 2,000 rows and 8 MB; excessive historical fields rejected, partial search disclosed |
| Target profile | 6,000 files, 64 manifests, 240,000 bytes/manifest; exceeding counts fails explicitly |
| Catalog concurrency | Short transactions and OS file lock, five-second wait; no DB lock during fetching or models |
| Source excerpts | Whole lines, at most 6,000 bytes per excerpt before numbered display; whole-file and excerpt hashes distinct |
| GitHub | Explicit search/inspection only, capped streamed REST responses; no automatic crawling or permanent add |
| Investigation | Seven calls by default, maximum twelve; finalization shares budget; 240-second maximum inner deadline, zero SDK retries |
| Anchors | 1–6 relative regular source files, range <=160 lines, optional validated symbol hint; never observation evidence |
| Navigation | rg, bounded repo-map and Python AST support; optional separate Serena process |
| Checks | This repository's Ruff/mypy/pytest, existing process control, bounded summaries, complete local logs |

Historical raw administrative export is row-paginated and preserves stored data;
it is not a source presentation endpoint. No new packaging feature is retained.
No automatic retention is added. Generated journals and snapshots can grow;
failed writes may leave a reusable immutable snapshot, never partial catalog rows.
