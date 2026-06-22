# Requirements: GitHub Project Finder MCP

> Historical MVP requirements. The active product direction is the catalog-first
> Next.js/React UI reuse layer described in `docs/source-scout-direction.md`.

## Functional Requirements

### Tool 1: `find_repos_for_task`

**Input:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Natural language task description |
| `language` | string | No | Programming language filter (e.g., Python, TypeScript) |
| `min_stars` | int ≥ 0 | No | Minimum star count |
| `max_age_days` | int ≥ 1 | No | Maximum days since last push |
| `license_filter` | string | No | SPDX identifier (e.g., mit, apache-2.0) |
| `limit` | int 1–10 | No (default 10) | Number of results to return |

**Output:** `FindReposResult`
- `query`: original task string
- `total_candidates_scored`: number of repos fetched and scored (max 30)
- `results`: list of `RepoSummary` (0–10 items)
- `cached`: whether search results came from cache
- `timestamp`: ISO 8601 timestamp

**RepoSummary fields:** `full_name`, `html_url`, `description`, `language`, `stars`, `last_push`, `score` (0.0–1.0), `verdict` ("useful"|"maybe"|"skip"), `risks` (list of strings)

**Behavior:**
- Fetches 30 results from GitHub search API
- Scores locally using ranker
- Returns top `limit` results
- Empty task → `ToolError("Task description is required")`
- No results found → empty list with `total_candidates_scored: 0`
- Rate limited → `RuntimeError` with `recoverable: true`, `retry_after` set

### Tool 2: `inspect_github_repo`

**Input:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `repo_url` | string | Yes | Full URL (`https://github.com/owner/repo`) or `owner/repo` string |

**Output:** `InspectionResult`
- `owner`, `repo`: string
- `description`, `language`, `license_name`: string | null
- `stars`, `forks`, `open_issues`: int
- `last_push`: ISO 8601 string
- `archived`: bool
- `structure`: `RepoStructure` (dirs, files, key_files)
- `quality`: `QualityReport` (signals dict, score float)
- `readme_preview`: first 500 chars of README | null
- `verdict`: "useful"|"maybe"|"skip"
- `verdict_reasoning`: string
- `cached`, `timestamp`

**Behavior:**
- Parses `owner/repo` from URL (supports both formats)
- Fetches metadata, README, file tree from GitHub API
- Evaluates quality signals: README presence/length, contributors, issue health, CI presence, license, activity
- Repo not found (404) → `ToolError("Repository not found or is private")`
- Private repo → same error (GitHub returns 404)
- Rate limited → `RuntimeError` with retry info

### Tool 3: `compare_github_repos`

**Input:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `repos` | list[str] | Yes (2–5 items) | Repo URLs or `owner/repo` strings |

**Output:** `CompareResult`
- `repos`: list of `CompareItem` (full_name, stars, activity, quality_score, license_name, verdict)
- `recommended`: full_name of best repo
- `reasoning`: explanation string
- `cached`, `timestamp`

**Behavior:**
- Inspects all repos in parallel
- Compares: stars, activity level ("active"|"moderate"|"stale"), quality score, license
- Recommends highest overall quality + activity repo
- Fewer than 2 repos → `ToolError("Provide 2–5 repositories to compare")`
- More than 5 repos → `ToolError("Maximum 5 repositories for comparison")`
- Any individual inspection fails → error propagated

### Tool 4: `extract_patterns_from_repo`

**Input:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `repo_url` | string | Yes | Full URL or `owner/repo` |
| `focus` | string | No | Focus area (e.g., "API design", "auth", "data pipeline") |

**Output:** `PatternReport`
- `owner`, `repo`: string
- `patterns`: list of `Pattern` (category, title, description, snippet, source)
- `file_tree`: list of top-level paths
- `readme_sections`: list of section titles found
- `focus`: string | null
- `verdict`: string
- `cached`, `timestamp`

**MVP Scope:**
- Parses README into sections (## headers)
- Lists top-level file tree
- Fetches first 30 lines of up to 5 key files
- Distills patterns from structured data
- Focus filters patterns by topic area
- No local cloning
- No tree-sitter parsing

**Pattern categories:** "architecture", "code_structure", "best_practice", "pitfall"

**Behavior:**
- Repo not found → `ToolError`
- No README → partial result with empty sections
- No recognizable patterns → empty patterns list with note

## Ranking Formula

Score each repo (0.0–1.0) using weighted sub-scores:

| Sub-Score | Weight | Factors |
|-----------|--------|---------|
| Relevance | 0.40 | Term overlap with query in description and topics. Language match if specified. |
| Activity | 0.20 | Pushed_at recency. Archived repos score 0. Penalized: 1yr+ = 0.3, 6mo+ = 0.6, 1mo+ = 1.0. |
| Popularity | 0.15 | log(stars+1) normalized against batch maximum. Forks factored at 0.3 weight. Capped at 5000 stars. |
| Structure | 0.15 | README present + length, topics present, license present. Evaluated from metadata. |
| License | 0.10 | Has license = 1.0, none = 0.2. Permissive (MIT/Apache/BSD) = 1.0, copyleft (GPL) = 0.8. |

Verdict mapping: `total ≥ 0.70 → "useful"`, `total ≥ 0.40 → "maybe"`, `total < 0.40 → "skip"`

## Cache Behavior

| Cache Type | Key Format | Default TTL | Env Var |
|------------|-----------|-------------|---------|
| Search results | `search:{sha256(query)[:16]}` | 1800s (30 min) | `CACHE_TTL_SEARCH` |
| Repo metadata | `repo:{owner}/{repo}` | 3600s (1 hour) | `CACHE_TTL_METADATA` |
| README content | `readme:{owner}/{repo}` | 7200s (2 hours) | `CACHE_TTL_README` |
| File tree | `tree:{owner}/{repo}:{path}` | Metadata TTL | `CACHE_TTL_METADATA` |

Cache is DuckDB in-memory by default. Set `CACHE_DB_PATH` for persistent storage.

## Error Response Format

All errors return structured JSON:
```json
{
  "error": "Human-readable error message",
  "recoverable": true,
  "retry_after": 45
}
```

- `ToolError` → user-correctable validation errors, shown to client
- `RuntimeError` → system errors (API failure, rate limit), may include retry info
- Stack traces are NEVER exposed in production

## Non-Goals

- No full GitHub repository crawling
- No indexing of all repositories
- No vector database or embeddings (can be added later)
- No local repository cloning (can be added in phase 2)
- No tree-sitter or AST parsing in MVP
- No private repository support (requires different auth scopes)
- No GitHub organization member analysis
- No code generation — patterns only
