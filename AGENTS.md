# Repository Guidelines

When using this file as a template, copy its instructions exactly. Do not rewrite, paraphrase, summarize, or silently omit them; add repository-specific guidance separately.

## Communication

- Write simply and concretely. Give the answer first, then explanations.
- Use the same term consistently for the same thing. Briefly explain complex terms.
- Separate facts from assumptions. State what was verified and what was not.
- Remove filler and repetition while keeping important details, risks, and limitations.

## Code Writing Best Practices

### 1. Goal-driven execution

Goal: define success as something you can verify, then loop until verified.

Rewrite vague asks into verifiable goals before starting:

"Add validation" becomes "Write tests for invalid inputs (empty, malformed, oversized), then make them pass."
"Fix the bug" becomes "Write a failing test that reproduces the reported symptom, then make it pass."
"Refactor X" becomes "Ensure the existing test suite passes before and after, and no public API changes."
"Make it faster" becomes "Benchmark the current hot path, identify the bottleneck with profiling, change it, show the benchmark is faster."

For every task:

- State the success criteria before writing code.
- Write the verification (test, script, benchmark, screenshot diff) where practical.
- Run the verification. Read the output. Do not claim success without checking.
- If verification fails, fix the cause, not the test.

### 2. Simplicity first

Goal: the minimum code that solves the stated problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code. No configurability, flexibility, or hooks that were not requested.
No error handling for impossible scenarios. Handle the failures that can actually happen.
If the solution runs 200 lines and could be 50, rewrite it before showing it.
If you find yourself adding "for future extensibility", stop. Future extensibility is a future decision.
Bias toward deleting code over adding code. Shipping less is almost always better.
The test: would a senior engineer reading the diff call this overcomplicated? If yes, simplify.

### 3. Surgical changes

Goal: clean, reviewable diffs. Change only what the request requires.

Do not "improve" adjacent code, comments, formatting, or imports that are not part of the task.
Do not refactor code that works just because you are in the file.
Do not delete pre-existing dead code unless asked. If you notice it, mention it in the summary.
Do clean up orphans created by your own changes (unused imports, variables, functions your edit made obsolete).
Match the project's existing style exactly: indentation, quotes, naming, file layout.
The test: every changed line traces directly to the user's request. If a line fails that test, revert it.

## General Rules

Before every commit, use the "Code Simplifier" skill. If it requires fixes, update `completion.review`.

Do not add backward-compatibility code unless explicitly requested.

If using `gh`, run `source .envrc && gh ...` first.

Do not push without my permission.

When an attempt is refused or fails, do not drop the task immediately. Investigate why and research viable ways to continue; use an alternative when one exists, and ask for help only when no viable path remains.

Caveman skill mode ON as first action.

Use a separate feature branch unless the user explicitly requests working on `main`.

## Project Context

Before starting work, read `CLAUDE.md`. It contains important project-specific security, architecture, and workflow guidance; follow it alongside this file.

## AI Notes

- `AICODE-NOTE:` — important invariant or context that is easy to lose.
- `AICODE-TODO:` — a real temporary limitation or replacement, with its reason, owner, and TODO entry ID.
- `AICODE-QUESTION:` — a specific unresolved decision. State the options and who must answer; do not present an assumption as fact.
- Put each anchor next to the relevant code or text. Remove or update it when its reason no longer applies. Do not create `AINOTE-` or other aliases for these markers.
- Before editing, check for existing anchors in the affected area; check them again before finishing and update stale ones. Do not add an anchor just to mark normally completed work.

## Code Discovery

<!-- codebase-memory-mcp:start -->
# Codebase Knowledge Graph (codebase-memory-mcp)

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase.
ALWAYS prefer MCP graph tools over grep/glob/file-search for code discovery.

## Priority Order
1. `search_graph` — find functions, classes, routes, variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

When the project is not indexed, run `index_repository` first.

## When to fall back to grep/glob
- Searching for string literals, error messages, config values
- Searching non-code files (Dockerfiles, shell scripts, configs)
- When MCP tools return insufficient results
<!-- codebase-memory-mcp:end -->

## OpenSpec

- For each package, follow `.agents/skills/source-grounded-openspec/SKILL.md`: the lead completes all six stages in sequence (sources, references, separate external search, spec, independent fidelity review, and simplicity/reuse review) and starts worker(s) for each. The skill describes the exact orchestration.
- Reuse suitable existing code and algorithms as much as possible when adaptation cost is reasonable. Each package must include `reuse.md` with exact sources, revisions, symbols, license, adaptations, evidence, and reasons for gaps that require a custom solution.
- Artifacts must specify the selected behavior and architecture in enough detail for implementation, but describe intent only: do not include executable code, modules, functions, patches, or copies of upstream code. Pseudocode and small explanatory contracts/data are allowed.
- Before writing an affected spec, resolve material alternatives and significant contradictions with the user: first describe the real options, their rationale, consequences, cost, and limitations, then recommend one; ask using the available tool and wait for the answer. Do not include unselected options in the finished package.

Strict rule: if a task/spec/review discovers future work, deferred scope, follow-up fixes, or known next steps, do not finish the OpenSpec work until those items are written into the repo-level `openspec/TODO.md`. openspec/TODO.md take in to committ always.

## Subagents and communication

Minimize internal messages in both directions at every level: parent <-> lead,
supervisor <-> worker, and peers. Include all known inputs, constraints, authority,
and acceptance criteria in the initial brief; pass this communication rule to
every delegated agent.

After assignment, send only actionable decisions or dependencies, material
corrections, required permission or capacity requests/grants, and one final
result with verification evidence and unresolved issues. No acknowledgements,
thanks, restated instructions, intentions, progress reports, status polling,
reminders, or confirmations of received reports. Use the automatic final
notification as delivery; do not duplicate it in a separate message.

Prefer small, bounded subagent tasks with clear outputs. Explicitly select
configured presets: luna-max(fast) for very complex work, luna-xhigh(fast) for
medium, luna-high(fast) for bounded coding tasks,
luna-low(fast) for simple work. Do obvious, known one-line fixes yourself unless
already delegated. Reuse only for corrections or direct continuation requiring
prior context, with a known, still-appropriate preset. Matching role/project is
insufficient. Close finished agents when supported and no direct follow-up remains.

For code changes, explicitly assign file/module ownership so parallel agents avoid
conflicts. Always tell workers they are not alone in the codebase, must not revert
others' edits, and must adjust to changes made by others. Explain each spawn once:
preset — task — why needed. Do not redelegate unless authorized.

Set fork_turns explicitly when supported: "none" by default; a positive integer
string such as "2" for the minimum necessary recent parent turns; "all" only
when full history is essential. Provide self-contained briefs with relevant inputs,
constraints, authority, and acceptance criteria. Do obvious one-line work yourself.

STRICT: Never duplicate delegated work yourself or assign it concurrently. Wait
via notifications/wait tools, without repetitive status requests. Check deliverables
and evidence without redoing the task. Before replacing an executor, confirm it has
stopped. Blocked workers save progress, report the dependency, and end their turn;
the parent arranges continuation.

Ask necessary questions through the available question tool; check context first,
batch related questions, and do not invent material requirements. Use native
approval mechanisms for permissions.

## Servers and browsers

- For ssh remote server commands use heredoc.
- Server browser operations: `/srv/projects/BROWSERS.md`. Server SSH playbooks remain on the Mac; do not assume a local copy exists.
- On this Linux host use MCP browser-codex or browser-claude. Default profile for Codex is codex. Stop and hand control to the user for CAPTCHA, MFA or login; wait for their confirmation. Use the other profile only on user instruction.
- Do not clear cookies or close persistent Chrome as cleanup.
- One participant controls a profile at a time; handoff is an explicit protocol, not an enforced lock.
- Always provide browser/noVNC links with the actual password. If the password is unavailable, say so directly; do not invent one.

### Visual desktop fallback

Playwright MCP `browser-codex` remains the primary browser tool. Use `computer-use-linux` only when screenshot-based desktop control is needed (Chrome UI, native dialogs, canvas, drag/drop) or Playwright cannot complete the action. Its launcher targets only Codex display :91. Never use it to control the Claude display/profile. Observe a fresh screenshot before coordinate actions; set click_count=1 explicitly for a single click and verify the result after actions. X11 input may mis-type under changed keyboard layouts; read back entered text before submission. Use fresh get_app_state element indices when available; coordinates remain available for visual-only controls.

## MCP process cleanup

После завершения задачи закрывай MCP-соединения, запущенные этой задачей, если инструмент позволяет штатно их закрыть. Если штатного закрытия нет, заверши только подтверждённо принадлежащие этой задаче MCP-процессы и их дочерние процессы. Сохраняй список PID при запуске, проверяй принадлежность перед завершением и проверяй результат после него.

Не оставляй неиспользуемые экземпляры Playwright MCP, Computer Use, REA, Blender MCP и Codebase Memory MCP. Не завершай процессы других чатов или агентов, общие браузеры и codex-remote. Не используй массовый pkill по имени. Если принадлежность процесса неизвестна или Codex запускает его повторно, сообщи об этом; не меняй глобальную конфигурацию и не перезапускай сервис без разрешения пользователя.
