# Source Scout direction and integration record

Codex owns reasoning, edits, test selection and final judgment. Source Scout
supplies deterministic check reports and an optional bounded investigator.
Exact text/file -> rg/direct read. Symbol/references -> Serena. Difficult,
unresolved cross-file/indirect/ownership/architecture relation -> selective investigator.
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

## Follow-up baseline and policy

The follow-up began on clean, pushed `main` at
`e4a437fc085b5252a34ce754aec2da4dc1422ca5`; a fresh fetch confirmed no divergence.
Baseline `python -m source_scout check --format json` passed Ruff, mypy and all
327 tests. The recorded checkout identity stayed unchanged. Baseline report:
`.source_scout/checks/f009c72085144da79abf65ea77ddf09d/report.json`.
No paid DeepSeek evals or live model requests were run in this follow-up.

The project now defaults to `[remote_exploration] mode = "selective"`.
Off returns disabled before model configuration, key access, model validation
or source collection. Selective requires a task, concrete reason, one of
`cross_file_contract`, `indirect_runtime_flow`, `ambiguous_ownership` or
`architecture_trace`, and at least one caller-declared method from `rg`,
`direct_read`, `serena`. These are routing/audit declarations, not proof of
execution. Serena is not a prerequisite. On permits an unclassified call with
task/reason and retains the same source, call and deadline limits.

`SOURCE_SCOUT_REMOTE_EXPLORATION=off|selective|on` overrides project policy;
false/0/no and true/1/yes retain off/on compatibility. An explicit TOML `mode`
wins over legacy `enabled = false/true`. Missing, unreadable or invalid policy
fails closed to off. The project-local Codex server has no permanent mode override.
Policy is read per investigation; normal selective use needs no config edit.
Unknown classification/method values are rejected, even if supplied in on mode.
See README for ordinary CLI/MCP parameters and environment override examples.

Only `explore_local_code` is exposed in the normal MCP profile. `model-status`
remains an explicit CLI debug command; the reuse profile retains `model_status`
and the four catalog tools. Explicit health/smoke checks are available in
selective/on mode and disabled in off mode; they are not part of normal routing.
Exploration journal v2 records policy mode, use case, local attempts and reason
alongside existing model, prompt/schema/analyzer versions, usage and stop data.

## Serena: installed and runtime-tested

Installed official `oraios/serena` revision
`be609b625740846dc2750cc13291966cf1216b00` into
`.source_scout/tools/serena-venv`, under the explicit follow-up approval.
Installed distribution: **serena-agent 1.7.1.dev0**. `pip`'s installed
`direct_url.json` records the exact approved commit.

Actual CLI output in this checkout: `Serena 1.7.1.dev0-e4a437fc-dirty`.
The Git suffix comes from Serena's installed `serena.util.git.get_git_status`
walking up to the containing Source Scout checkout; it is not the Serena
installation revision. The installed distribution metadata proves the pin.
The installed CLI's version, startup options and `tools list --help/--all`
were exercised and saved, rather than inferred from documentation.

The separate Python backend is **Pyright 1.1.403**, selected by pinned Serena.
Its required **uv 0.12.10** launcher was installed in the Serena environment.
`UVX` points at that isolated launcher; uv package and Python caches are under
`.source_scout/tools/`. `SERENA_HOME` is `.source_scout/serena-home`. No Source
Scout dependency, global installation or global Codex configuration was changed.
`.serena/project.yml` did not exist and was safely created from the prepared
read-only project template. Existing files must be merged on subsequent setups.

Project-local `.codex/config.toml` now enables Serena, following successful
installation and startup. It supplies an explicit checkout root/cwd, no dashboard
or GUI window, the fixed server-side context, and the matching Codex allowlist:

`initial_instructions`, `get_current_config`, `get_symbols_overview`,
`find_symbol`, `find_referencing_symbols`, `find_implementations`, `find_declaration`.

Runtime `tools/list` contains exactly those seven tools. No editing, shell,
memory-writing/onboarding, generic file search/read or diagnostics tools are
exposed. Tool restriction is not an OS security sandbox. Ruff/mypy/pytest remain
authoritative; Source Scout does not invoke Serena or provide an LSP abstraction.

Two prepared-config assumptions were corrected using actual runtime evidence:

- Serena's `single_project: true` also removes `get_current_config`. The context
  now sets it false so root/config inspection remains possible. `fixed_tools`
  excludes `activate_project` and all cross-project query tools, so this session
  still cannot switch from the explicitly selected Source Scout project.
- `--mode no-memories` does not remove Serena's default base modes. The isolated
  `SERENA_HOME/serena_config.yml` now has `base_modes: []`. The final server
  reports only `no-memories`, with the seven-tool allowlist unchanged.

Actual acceptance results, using the final config and a real stdio MCP session:

| Check | Evidence/result |
| --- | --- |
| MCP initialize and tools/list | Started; exact seven-tool set asserted. |
| Active project/root | `get_current_config`: source_scout, LSP ready; startup log: Python server for `C:\AI\Dev\source_scout`. |
| Symbol overview | `get_symbols_overview` on `src/source_scout/cli_checks.py` returned `_check_commands`. |
| Definition | `find_symbol` returned the real `_check_commands` function and body. |
| References | Five references across cli_checks.py, __main__.py and test_check_cli.py; each returned source line matched current bytes/rg after converting LSP's zero-based lines. Included `_run_check_commands`. |
| Declaration | `find_declaration` at the `_check_commands(with_local_explore_eval, ...)` call resolved the function/body. |
| Implementations | Actually called. Pyright returned `textDocument/implementation` method-not-found (-32601); this backend does not support it. No successful implementation lookup is claimed. |
| Same-session freshness | Appended a temporary function and caller, found both; renamed both and found the new symbol/reference while the old symbol disappeared. Restored original bytes and confirmed the new symbol disappeared in the same session. |
| Cleanup | Finally-style byte restoration, equal SHA-256 before/after, unchanged Git status across verification. |

Sanitized evidence lives in the ignored `.source_scout/serena-verification/`:
`runtime.json` (CLI, install metadata, actual schemas/results, comparison and
cleanup), `server.stderr.log` (backend/root/startup), `verify_runtime.py` (manual
harness), and `codex-config.json` (effective project configuration).
Codex app-server `config/read` confirms this trusted project layer loads Serena
enabled and Source Scout restricted to the investigator. This is fresh config
verification, not a claim that an already-open IDE session has reconnected.

## Ordinary use and reproducible Serena setup

For this checkout, restart/reconnect the project's MCP servers once to load the
new configuration. Use `/mcp` in Codex to inspect connections. No global config
edit or normal per-investigation toggle is required.

```powershell
.\.venv\Scripts\python.exe -m source_scout check --format json
.\.venv\Scripts\python.exe -m source_scout explore-local --project-path . --task 'Trace the unresolved caller contract' --reason 'Local references leave the return contract unclear' --use-case cross_file_contract --attempted-local-method rg --attempted-local-method direct_read
.\.venv\Scripts\python.exe -m source_scout model-status
```

The investigator needs an inherited `DEEPSEEK_API_KEY` for allowed remote calls.
Never commit its value. For MCP, call `explore_local_code` with `task`,
`project_path`, `reason`, `use_case`, and `attempted_local_methods` as a list.
Seven model requests remain the default (hard cap twelve); finalization shares
the cap, SDK retries are zero, and total deadlines remain bounded.

To reproduce the isolated installation in this checkout:

```powershell
.\.venv\Scripts\python.exe -m venv .source_scout/tools/serena-venv
.\.source_scout\tools\serena-venv\Scripts\python.exe -m pip install 'git+https://github.com/oraios/serena.git@be609b625740846dc2750cc13291966cf1216b00' 'uv==0.12.10'
$env:SERENA_HOME = 'C:\AI\Dev\source_scout\.source_scout\serena-home'
$env:UVX = 'C:\AI\Dev\source_scout\.source_scout\tools\serena-venv\Scripts\uvx.exe'
$env:UV_CACHE_DIR = 'C:\AI\Dev\source_scout\.source_scout\tools\uv-cache'
$env:UV_PYTHON_INSTALL_DIR = 'C:\AI\Dev\source_scout\.source_scout\tools\uv-python'
.\.source_scout\tools\serena-venv\Scripts\serena.exe --version
.\.source_scout\tools\serena-venv\Scripts\serena.exe start-mcp-server --help
```

Merge `config/serena-project.yml` into `.serena/project.yml` (copy only when
absent). Set `base_modes: []` in the isolated
`.source_scout/serena-home/serena_config.yml`, preserving other settings; if the
file is absent, a file containing just that key suffices for the normal defaults.
Start with the exact args/env from `.codex/config.toml` and repeat the acceptance
steps above before enabling a new installation. The manual harness is local
verification evidence, not a Serena unit-test suite. Adjust the absolute paths
for another checkout. Disable only this project's Serena table to undo the
integration; generated state need not be deleted.

## Older global FastContext skill migration

Do not change `C:/Users/Nikla/.codex/skills/fastcontext-local/SKILL.md` globally
as part of this implementation. For this repo, AGENTS.md and current user
instructions take precedence. A future global update should make these exact
behavior changes:

- Replace the cold-start/multi-file automatic trigger with selective use for an
  unresolved question after appropriate rg or Serena navigation.
- Standing permission remains valid, but require the off/selective/on project/process policy;
  the presence of `DEEPSEEK_API_KEY` is insufficient.
- Add reason, use case and attempted local methods to MCP calls and CLI examples.
- Remove automatic `model_status(smoke_test=true)`, CLI retry/escalation and
  second-investigator instructions on sparse/error results. Disabled/unavailable
  means return to local tools. Model health/smokes are explicitly selected actions.
- Keep the seven-turn default and tight cited reads. Treat incomplete/budget
  results as such; do not silently raise budgets or claim complete evidence.
- Follow `report_path` for local usage details; never infer savings from file
  counts or output length. Do not duplicate a future Preflight integration.

## Evaluation limits and next evidence gate

The current local-explore path/line hit metric is a **navigation metric**. It
does not establish better finished patches, lower overall Codex cost or
preservation of final-result quality. The old eval runner is unchanged: its
unclassified tasks require explicitly forced on mode, not invented selective
classifications. Do not run paid suites automatically or claim a savings percentage.

Check/exploration run directories have no automatic retention and can grow
without bound. Retention is deferred; this iteration performs no automatic deletion.

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
