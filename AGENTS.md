# Project Instructions

Local-first agentic development with a structured Plan → Execute → Review workflow.

## Workflow

1. **Plan** — `/plan` or switch to the plan agent for design and architecture. Never code first.
2. **Execute** — Delegate implementation steps to the worker agent. One step at a time.
3. **Review** — `/review` after every meaningful change. Address CRITICAL issues before proceeding.

## Agents

| Agent | Mode | Model | Use for |
|-------|------|-------|---------|
| plan | primary | deepseek-v4-pro | System design, architecture, planning |
| ask | primary | deepseek-v4-flash | Code explanation, questions, research |
| reviewer | subagent | deepseek-v4-pro | Code review (read-only) |
| worker | subagent | deepseek-v4-flash | Implementation of defined tasks |
| code (default) | primary | deepseek-v4-flash | Execution orchestration, delegating to worker |
| explore | subagent | deepseek-v4-flash | Codebase exploration, searching |

## Skills

Add project-specific skills to `.kilo/skills/`. Each skill is a directory with a `SKILL.md` file. The directory name becomes the skill identifier (`/skill <name>`). Skills should encode patterns the LLM cannot infer from your codebase — skip anything generic (debugging, TDD, code review). An example stub is provided in `.kilo/skills/example/`.

## Continuous Improvement Loop

1. **Weekly quality check** — `scripts/run_quality_checks.py` runs against the ground-truth corpus, reports pass/fail per repo.
2. **Review findings** — Integration test failures indicate stale ground truth (repos change over time) or regressions in the code.
3. **Adjust weights** — Ranker weights are configurable via `RANKER_WEIGHT_*` env vars. No code change needed.
4. **Fix framework detection** — `_FRAMEWORK_MARKERS` in `framework_detector.py` accepts new markers.
5. **Tune thresholds** — Verdict thresholds in `ranker.py`, `repo_inspector.py`, and `pattern_extractor.py` are hardcoded constants.
6. **Update corpus** — When ground truth becomes stale (e.g., license or stars changed), update `tests/corpus/ground_truth.json`.
7. **Commit fixes** — The CI workflow validates the fix.

## Do NOT

- **Do not jump to implementation without a plan.** Non-trivial changes require a written plan first.
- **Do not add dependencies or libraries without discussion.**
- **Do not refactor unrelated code.**
- **Do not leave debug logs, TODO comments, or commented-out code.**
- **Do not skip linting, type-checking, or tests.** Work is not done until all three pass. After 2 failed fix attempts, escalate.
