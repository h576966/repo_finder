# Source Scout direction and integration record

Codex owns reasoning, edits, test selection and final judgment. Source Scout
supplies deterministic check reports and an optional bounded investigator.
Exact text/file -> rg/direct read. Symbol/references -> Serena. Difficult,
unresolved multi-file trace -> selectively enabled investigator with a reason.
No mandatory tool loop, reminder hooks, hosted review or automatic integration.

## Local reality and compatibility

Work began on `main` at `48c887b03b347fb1baf79fd56306053905876496`, with a clean
working copy. The earlier review SHA `de47643566aba346193ecba5a60b6e333499f9b9`
was not used as the implementation baseline. This direction document and
`complexity-budget.md` did not exist in that checkout. Baseline: Ruff/mypy pass,
269 pytest tests pass. No paid live evaluations were run.

The catalog remains functional behind explicit legacy selection; development
is frozen and data is preserved. The README documents changed defaults,
commands and rollback. The deterministic check wrapper remains a terminal CLI.
Exploration cannot decide that a change is correct or omit necessary testing.

## Serena: configured versus tested

Inspected official source revision:
`be609b625740846dc2750cc13291966cf1216b00`.
Its `pyproject.toml` declares `1.7.1.dev0`. This is a source version, not an
installed/runtime version. The installed Codex CLI reports `0.153.0`.
Serena, uv and uvx were absent from PATH; no Serena or Preflight MCP server was
found in the available tool list or the inspected global Codex MCP entries.
The global `fastcontext-local` skill was present and inspected.

Configured:

- Project-local `.codex/config.toml` for the Codex VS Code extension/CLI,
  not VS Code's separate Copilot MCP client. Explicit absolute project root/cwd.
- Serena entry disabled pending installation/verification. Isolated virtual
  environment path under `.source_scout/tools/serena-venv`; separate
  `SERENA_HOME` under `.source_scout/serena-home`. Global settings untouched.
- `config/serena-context.yml` uses server-side `fixed_tools` and a matching
  Codex allowlist: `initial_instructions`, `get_current_config`,
  `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`,
  `find_implementations`, `find_declaration`. Single project, no switching.
- `no-memories` mode replaces the default modes; no shell, editing, overlapping
  file tools or memory-writing/onboarding tools. No hooks. A read-only project
  config template is in `config/serena-project.yml`.
- Diagnostics deliberately omitted until the selected language backend's
  support is confirmed at runtime. Ordinary Ruff/mypy checks remain authoritative.

Verified: official revision resolution, source-declared version, CLI option
names, fixed-tool configuration keys and symbol-tool names/signatures. The
installed Codex app server's `config/read` with this cwd loaded the project
layer with no disabled reason: Source Scout enabled with the sidecar allowlist,
Serena disabled with the seven-tool allowlist. The sanitized result is saved at
`.source_scout/serena-verification/codex-config.json`. The sandbox's separate user
profile lacked project trust; verification used the real user's existing trust
without changing global settings. Only read-only upstream source inspection was performed.
No downloaded Serena code was executed. Tool restriction is not a security sandbox.

Not verified: server startup, actual MCP `tools/list`, language-server startup,
active root returned by the server, real symbol/reference results and freshness
after local edits. Installation was rejected by automatic approval review:
fetching/executing external packaging code was judged contrary to the repository
rule against executing arbitrary cloned code. The empty isolated environment
was created before rejection; Serena was not installed. No alternate installation
route was attempted. Further installation needs explicit approval for that action.

## Reproducible installation and verification after approval

The prepared commands install only the pinned Serena revision into its own
environment; they do not add a dependency to Source Scout. Language-server setup
may require further downloads under the host's normal approval flow.

```powershell
.\.venv\Scripts\python.exe -m venv .source_scout/tools/serena-venv
.\.source_scout\tools\serena-venv\Scripts\python.exe -m pip install 'git+https://github.com/oraios/serena.git@be609b625740846dc2750cc13291966cf1216b00'
.\.source_scout\tools\serena-venv\Scripts\serena.exe --version
.\.source_scout\tools\serena-venv\Scripts\serena.exe start-mcp-server --help
.\.source_scout\tools\serena-venv\Scripts\serena.exe tools list --all
New-Item -ItemType Directory .serena -Force
Copy-Item config/serena-project.yml .serena/project.yml
```

Confirm the tool-list flag against the pinned `tools list --help` before use.
If `.serena/project.yml` already exists, merge the shown settings instead of
overwriting it. Then set `mcp_servers.serena.enabled = true` in
`.codex/config.toml`, restart the Codex extension and inspect `/mcp`.
For another checkout location, update the absolute paths in that file.

Runtime acceptance steps (record raw results under `.source_scout/serena-verification/`):

1. Capture `tools/list`; require the configured seven-tool allowlist and absence
   of editing, shell and memory tools. Capture initial instructions and check
   they describe the available tools.
2. Call `get_current_config`; verify active `source_scout` project and root
   `C:\AI\Dev\source_scout`, checking server startup logs if root is not in output.
3. Call `get_symbols_overview(relative_path="src/source_scout/cli_checks.py")`.
   Call `find_symbol(name_path_pattern="_check_commands",
   relative_path="src/source_scout/cli_checks.py", include_body=true)`.
4. Call `find_referencing_symbols(name_path="_check_commands",
   relative_path="src/source_scout/cli_checks.py")`; verify the real reference
   from `_run_check_commands`. Compare with rg/source. Test declaration and
   implementation lookup on backend-supported symbols; record unsupported cases.
5. Save original bytes of a small source fixture, add a temporary symbol and a
   caller, query both, rename them locally and query again in the same session.
   Require the new symbol/reference and absence of the old one. Restore the
   exact bytes in a `finally` block and verify Git status. Do not claim freshness
   from a restarted process alone.

Disable/remove only the project's Serena table to undo integration. Removing
its generated environment/state is optional and never required for rollback.

## Older global FastContext skill migration

Do not change `C:/Users/Nikla/.codex/skills/fastcontext-local/SKILL.md` globally
as part of this implementation. For this repo, AGENTS.md and current user
instructions take precedence. A future global update should make these exact
behavior changes:

- Replace the cold-start/multi-file automatic trigger with selective use for an
  unresolved question after appropriate rg or Serena navigation.
- Standing permission remains valid, but require enabled project/process policy;
  the presence of `DEEPSEEK_API_KEY` is insufficient.
- Add `reason` to MCP calls and `--reason` to CLI examples.
- Remove automatic `model_status(smoke_test=true)`, CLI retry/escalation and
  second-investigator instructions on sparse/error results. Disabled/unavailable
  means return to local tools. Model health/smokes are explicitly selected actions.
- Keep the seven-turn default and tight cited reads. Treat incomplete/budget
  results as such; do not silently raise budgets or claim complete evidence.
- Follow `report_path` for local usage details; never infer savings from file
  counts or output length. Do not duplicate a future Preflight integration.

## Small manual comparison (not run)

Use three real tasks from the same starting checkout in isolated working copies:
(1) repair a CLI error-propagation bug, (2) trace and fix citation validation,
(3) change a caller contract and its relevant tests. For each task compare:

| Variant | Tools |
| --- | --- |
| A | Codex + rg, ordinary checks |
| B | Same + Serena and persisted check reports |
| C | Same as B + selectively enabled DeepSeek with recorded reason |

Record starting SHA/working-copy identity, finished behavior, regression results,
manual-review findings, rework count, wall time, available Codex and Source Scout
usage, unknown usage fields, and investigation stop causes. Use the same task
acceptance requirements in all variants; do not cut tests to save tokens.
Compare final outcome first, then effort/latency/usage. Do not count cached input
twice or infer a saving percentage from one component's usage. This is a manual
worksheet, not a new eval framework; paid comparisons require explicit selection.

## Official references consulted

- [Serena clients](https://oraios.github.io/serena/02-usage/030_clients.html)
- [Serena tools](https://oraios.github.io/serena/01-about/035_tools.html)
- [Serena configuration](https://oraios.github.io/serena/02-usage/050_configuration.html)
- [Pinned Serena context template](https://github.com/oraios/serena/blob/be609b625740846dc2750cc13291966cf1216b00/src/serena/resources/config/contexts/context.template.yml)
- [Pinned Serena CLI](https://github.com/oraios/serena/blob/be609b625740846dc2750cc13291966cf1216b00/src/serena/cli.py)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp)
