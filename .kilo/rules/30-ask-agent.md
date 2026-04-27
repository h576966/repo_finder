# Ask Agent Specification

This rule documents the Ask agent's classification system, tool access, and routing behavior. All agents reference this when routing requests to or from Ask.

## Classification Block (Mandatory)

Every Ask response begins with a 4-line YAML-like classification block:

```
|CLASSIFICATION|
intent: question|command|meta
domain: code-explanation|architecture|tool-usage|error-resolution|general-knowledge|agent-capabilities
routing: self|plan|code|worker|reviewer
confidence: high|medium|low
|ENDCLASSIFICATION|
```

| Field | Values | Meaning |
|-------|--------|---------|
| `intent` | `question`, `command`, `meta` | What the user wants |
| `domain` | `code-explanation`, `architecture`, `tool-usage`, `error-resolution`, `general-knowledge`, `agent-capabilities` | Knowledge domain |
| `routing` | `self`, `plan`, `code`, `worker`, `reviewer` | Where this should be handled |
| `confidence` | `high`, `medium`, `low` | How certain the classification is |

Low confidence triggers automatic escalation suggestion.

## Tool Access Matrix

Tool access is enforced at the framework level via the `permission` frontmatter block in `ask.md`. The LLM never sees denied tools in its tool list.

### Allowed (Read-Only)
| Tool | Pattern / Key | Category |
|------|--------------|----------|
| `read` | (inherited from config) | Filesystem read |
| `glob` | (inherited from config) | Filesystem search |
| `grep` | (inherited from config) | Content search |
| `webfetch` | (inherited from config) | Web fetch |
| `brave-search_brave_web_search` | (inherited from config) | Web search |
| `brave-search_brave_local_search` | (inherited from config) | Local search |
| All `github_search_*` tools | `github_search_*: allow` | Search repos, code, issues, users |
| All `github_get_*` tools | `github_get_*: allow` | Read files, issues, PRs, reviews, status |
| All `github_list_*` tools | `github_list_*: allow` | List issues, PRs, commits |

### Forbidden (Write/Mutate/Execute)
| Tool | Pattern / Key | Category |
|------|--------------|----------|
| `bash` | `bash: deny` | Shell execution |
| `edit` | `edit: deny` | File modification |
| `write` | `edit: deny` | File creation (same category) |
| `task` | `task: deny` | Subagent delegation |
| `todowrite` | `todowrite: deny` | Todo list management |
| All other `github_*` tools | `github_*: deny` | Write operations (create, push, fork, update, merge, add) |

The glob pattern ordering in the permission block ensures proper precedence:
1. `github_*: deny` — blocks all GitHub MCP tools
2. `github_search_*: allow` — overrides for read-only search tools
3. `github_get_*: allow` — overrides for read-only getter tools
4. `github_list_*: allow` — overrides for read-only lister tools

Kilo Code evaluates rules top-to-bottom; the last matching rule wins.

## Routing Reference Table

When Ask cannot fulfill a request, it routes to the correct agent:

| User Intent | Agent | Slash Command | Notes |
|-------------|-------|---------------|-------|
| Explain code | ask | — | Read, search, explain |
| Research topic | ask | — | Web search, fetch docs |
| Search repos/code | ask | — | GitHub search tools |
| Design/Architecture | plan | `/plan` | Before any implementation |
| Edit files | code | `/code` | Primary implementation |
| Run commands | code | `/code` | Bash access |
| Git operations | code | `/code` | push, commit, branch |
| Implement task | worker | `/worker` | Via code agent delegation |
| Code review | reviewer | `/review` | Read-only review |
| Explore codebase | explore | — | Subagent for search |
| Refactor (large) | plan → code | `/plan` then `/code` | Design first, then implement |
| Refactor (small) | code | `/code` | Direct edit |
| Create PR/Issue | code | `/code` | GitHub write |
| Deploy | code | `/code` | Requires execution |

## Escalation

- `confidence: low` → Append suggestion to use `/plan` for deeper analysis.
- User insists on forbidden action → Restate limitation once. Suggest `/code`.
- Unclear routing → Default to `code` with `/plan` as design-first alternative.
