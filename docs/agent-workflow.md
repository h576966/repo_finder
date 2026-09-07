# Codex and ChatGPT handoff

This project uses one root `HANDOFF.md`, replaced after a coherent work unit that
changes project files. It is a short current-state summary, not a changelog or
agent transcript. `AGENTS.md` owns the lifecycle and validation policy. Older
snapshots remain in Git; retain an older decision only while it still matters.

## Setup and boundaries

Codex's global instructions maintain handoffs in repositories that opt in; this
repository also states the policy locally so it travels with the project.
`git-ship` handles authorized delivery, not normal implementation completion.
No hook, scheduler, new skill or runtime dependency is involved. Instructions
guide behavior; they do not enforce an automatic filesystem update.

Global instructions and `git-ship` live outside this repository. A repository
commit does not back them up or deploy them on another machine. The Source Scout
skills' source of truth remains `plugins/source-scout`; use the existing installer
to refresh the installed plugin and start a new Codex task to pick up changes.

ChatGPT must explicitly read the handoff through an available GitHub connection
or an attachment. Unpushed edits and local `.source_scout/` reports are not made
available by a GitHub read. Summarize material evidence in the handoff using
repo-relative paths; omit secrets, raw excerpts and local-only links as the sole
support for a claim. Never claim current CI/push status from the snapshot alone.

The following text is ready for the Source Scout ChatGPT project's instructions.
It is not installed into ChatGPT by saving this file or editing a local mirror.

## ChatGPT project instruction (copy this section)

Du hjälper mig som Ask Agent att granska Source Scout och formulera avgränsade
uppgifter till Codex. Projektet är ett personligt, lokalt Windows-verktyg, inte
avsett för publik distribution. Prioritera enkelhet, robusthet och evidensbaserade
generella förbättringar; undvik spekulativa heuristiker och överbyggnad.

Vid projektrelaterad analys eller nästa Codex-uppgift: använd den anslutna
GitHub-källan för h576966/source_scout och läs HANDOFF.md, AGENTS.md samt relevant
diff/källkod från samma aktuella revision. Utgå från main om jag inte anger en
annan branch. Redovisa vilken revision underlaget gäller. En bifogad lokal
handoff kan beskriva opushat arbete: håll det separat från verifierat GitHub-läge.
Om åtkomst saknas, säg vad som inte kan verifieras och be endast om det underlag
som behövs. Utgå inte från att gamla samtal eller projektfiler är aktuella.

Handoff är en daterad sammanfattning, inte bevis för nuvarande CI-status eller
tillstånd att genomföra föreslagna nästa steg. Skilj kontrollerade fakta från
Codex bedömningar, obevisad nytta och kvarstående frågor. En review eller begäran
om en prompt ska inte i sig ändra filer, skapa tasks, göra commit eller push.

När du föreslår en Codex-prompt, ge en färdig prompt med mål, relevant kontext,
avgränsning och proportionerlig verifiering. Lägg rekommenderad modell och effort
med en kort motivering utanför prompten. Utgå från aktuellt tillgängliga modeller
och officiell information vid osäkerhet; gissa inte tillgänglighet. Välj efter
uppgiftens osäkerhet och risk, inte automatiskt högsta effort. Respektera mitt
uttryckliga modellval. Rekommendationer ändrar inte inställningar eller mandat.

## Bounded acceptance checks

Use these scenarios for a walkthrough now and observed usage later. Do not add
tests that merely match instruction wording. A walkthrough is not independent
agent execution, and unit tests do not prove instruction-following quality.

| Scenario | Observable acceptance |
|---|---|
| Review or Ask only | Findings/prompt returned; worktree and handoff unchanged |
| Implementation, no commit | Handoff updated with evidence and limits; no commit/push |
| Commit and push | Only intended changes plus handoff included; actual push outcome reported |
| Push-only, dirty tree | Only existing commits sent; index, working files and handoff untouched |
| Unrelated staged changes | Not silently committed or reset; scope resolved with user |
| Required test fails | Ordinary delivery stops; explicit permitted checkpoint disclosed as unvalidated |
| Only final handoff results change after check | Documentation diff reviewed; report keeps original identity |
| Fresh Ask conversation | Reads a known revision, retains uncertainty, proposes bounded task plus model/effort |

Try the fresh-Ask scenario after the handoff has been pushed or attached. Record
observed omissions before adjusting the instructions. Do not claim the workflow
is proven merely because this checklist exists.

Instruction placement follows [OpenAI's AGENTS.md guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
