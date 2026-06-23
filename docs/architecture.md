# Architecture: GitHub Project Finder MCP

> Historical MVP architecture. The active architecture direction is the
> catalog-first reuse layer described in `docs/source-scout-direction.md`.

## File Tree

```
src/source_scout/
    models.py              # Shared dataclasses for all modules
    github_client.py       # GitHub REST API client (httpx)
    cache.py               # DuckDB-based query/result cache
    ranker.py              # Multi-factor repo scoring (pure functions)
    repo_inspector.py      # Deep repo analysis (uses client + ranker)
    pattern_extractor.py   # README + file tree pattern extraction
    server.py              # FastMCP server + tool definitions
    __main__.py            # CLI entry point
tests/
    conftest.py            # Pytest fixtures (GITHUB_TOKEN gate)
    test_github_client.py
    test_cache.py
    test_ranker.py
    test_repo_inspector.py
    test_pattern_extractor.py
    test_server.py
    test_integration.py
docs/
    requirements.md
    architecture.md
```

## Data Flow

```
┌─────────────────────────────────────────────────┐
│              MCP Client / Coding Agent           │
│   find_repos → inspect → compare → extract       │
└──────────────────┬──────────────────────────────┘
                   │ JSON-RPC (stdio)
┌──────────────────▼──────────────────────────────┐
│              server.py (FastMCP)                 │
│                                                  │
│  find_repos_for_task()                          │
│    ├── cache.get("search:...")                  │
│    │     ├── HIT  → cached result               │
│    │     └── MISS → github_client.search_repos()│
│    │                └── cache.set(...)           │
│    └── ranker.rank_repos(results, query)        │
│         └── FindReposResult                     │
│                                                  │
│  inspect_github_repo()                          │
│    ├── cache.get("repo:owner/name")             │
│    │     ├── HIT  → cached result               │
│    │     └── MISS → repo_inspector.inspect()    │
│    │                ├── get_repo_metadata()      │
│    │                ├── get_readme()             │
│    │                ├── get_repo_contents()      │
│    │                └── evaluate_quality()       │
│    │                └── cache.set(...)           │
│    └── InspectionResult                          │
│                                                  │
│  compare_github_repos()                          │
│    └── asyncio.gather(inspect_repo(r) for r)    │
│         └── CompareResult                        │
│                                                  │
│  extract_patterns_from_repo()                    │
│    ├── cache.get("readme:owner/name")            │
│    ├── parse_readme_sections()                   │
│    ├── collect_key_file_previews()               │
│    └── distill_patterns()                        │
│         └── PatternReport                        │
└──────────────────┬──────────────────────────────┘
                   │ httpx (async)
┌──────────────────▼──────────────────────────────┐
│           github_client.py                       │
│  _get(endpoint) → checks rate limits → returns   │
│  search_repos()  get_repo_metadata()             │
│  get_readme()    get_commits()                   │
│  get_repo_contents()  get_file_content()         │
└──────────────────┬──────────────────────────────┘
                   │ HTTPS
┌──────────────────▼──────────────────────────────┐
│           api.github.com (REST API)              │
│  /search/repositories                           │
│  /repos/{owner}/{repo}                          │
│  /repos/{owner}/{repo}/readme                   │
│  /repos/{owner}/{repo}/commits                  │
│  /repos/{owner}/{repo}/contents                 │
└─────────────────────────────────────────────────┘
```

## Module Dependencies

```
server.py
  ├── models.py          (no deps)
  ├── cache.py           ├── models.py
  ├── ranker.py          ├── models.py
  ├── github_client.py   ├── models.py
  │                      └── cache.py
  ├── repo_inspector.py  ├── models.py
  │                      ├── github_client.py
  │                      └── ranker.py
  └── pattern_extractor.py  ├── models.py
                            └── github_client.py

Dependency direction: server → inspector → client → cache
                    server → pattern_extractor → client → cache
                    server → ranker
```

`models.py` is a leaf module with zero internal dependencies. All other modules import from it.

## Error Propagation

```
github_client.py
  RateLimitError      → server catches, wraps in RuntimeError(json)
  httpx.HTTPError     → server catches, wraps in RuntimeError(json)

ranker.py
  (pure functions, no errors)

repo_inspector.py
  errors from github_client → bubble up

pattern_extractor.py
  errors from github_client → bubble up

server.py
  ToolError           → raised to FastMCP (user-visible)
  RateLimitError      → RuntimeError(json{"error", "recoverable": true, "retry_after"})
  httpx.TimeoutException → RuntimeError(json{"error", "recoverable": true, "retry_after": null})
  Exception           → RuntimeError(json{"error", "recoverable": false})
```

## Testing Strategy

**Unit tests** (`tests/test_*.py`):
- Mock httpx transport with `respx` or custom `httpx.AsyncTransport`
- Fixture-based data for ranker, pattern extraction
- Cache hit/miss/expiry behavior
- No real network calls

**Integration tests** (`tests/test_integration.py`):
- Marked `@pytest.mark.integration`
- Skipped if `GITHUB_TOKEN` env var is not set
- Test: search returns results, inspect returns structured data, compare works, extract returns patterns
- Verifies cache behavior (second call returns `cached: true`)

**Running tests:**
```bash
pytest -q                    # current Source Scout path; excludes integration and legacy
pytest -q -m "integration"   # live GitHub tests (needs GITHUB_TOKEN)
pytest -q -m "legacy"        # hidden/debug generic repository tools
```

## Concurrency

- `server.py` tools are `async def` — non-blocking
- `httpx.AsyncClient` used for all HTTP calls
- `compare_github_repos` uses `asyncio.gather` for parallel inspections
- DuckDB connections are per-module (not shared across threads)
- Cache operations are serial (DuckDB is not async-native; wrapped with sync calls)

## Cache Design

```
┌─ cache.py ──────────────────────────┐
│ init_cache() → connection            │
│ cache_get(key) → dict | None         │
│   ├── SELECT value WHERE key=?      │
│   └── IF now - created_at > ttl:     │
│        DELETE, return None           │
│ cache_set(key, value, ttl)           │
│   └── UPSERT                        │
│ cache_delete(key)                    │
│ get_ttl(type) → seconds              │
└─────────────────────────────────────┘

Schema:
  CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds INTEGER NOT NULL
  )
```

Cache is in-memory by default (connection string `:memory:`). Optional `CACHE_DB_PATH` env var enables file-backed persistence.
