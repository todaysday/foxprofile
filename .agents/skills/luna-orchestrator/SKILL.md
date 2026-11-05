---
name: luna-orchestrator
description: "Explicit-only orchestration: active parent decisions, bounded delegation, optional small team leads, and quiet execution. Use only when the user explicitly invokes this skill."
---

# Luna Orchestrator

Apply to the explicitly selected task and its direct follow-ups, not unrelated
requests. User and repository requirements override this skill's workflow
defaults; nothing here relaxes higher-priority instructions. Keep the base
subagent and communication rules from AGENTS.md. Invocation selects a workflow;
it does not authorize additional scope.
Keep orchestration guidance here or in focused references; reserve AGENTS.md for
shared rules that must apply outside this explicit-only workflow.

## Parent: decide, delegate, accept

Act as the orchestrator, not a dispatcher or default executor. Own the approach,
priorities, decomposition, dependencies, cross-task decisions, and acceptance.
Before launch, reconcile remaining scope against authoritative sources (such as
an index, TODO, active packages, or relevant reports); inspect dependencies,
choose ready packages, and resolve discrepancies before assuming inventory.
Identify applicable acceptance gates and minimum evidence before briefing. Keep
implementation, review, fixes, verification, archive, simplification, and commit
as separate states only when the actual task or repository requires them; do not
impose a universal coding sequence or fixed full-suite count. Archive only
completed changes, then check the resulting living spec when OpenSpec applies.

Read enough context to decide; delegate detailed investigation, implementation,
testing, and documentation. Do only obvious, known, unassigned one-line fixes
yourself.

Before delegating, define a small package: concrete outcome, scope, inputs,
acceptance evidence, and stop condition. Split independent outcomes yourself.
Never hand the entire request or an open-ended project phase to a team lead.

Choose next steps from evidence, resolve conflicting findings, and revise the
plan. Check returned changes and verification without repeating the investigation.
Accept compact receipts in existing canonical locations, linking existing test
outputs; use completion.review only when the project requires it. Avoid per-slice
journals, new ledgers, and duplicate logs by default. Keep an in-memory ledger if
useful. Request specific corrections or targeted validation after delivery. Do
not merely relay reports, accept unsupported conclusions, or duplicate active
work.

## Agent naming

Use human labels with a stable numeric package/slice ID:

- `TLXXX <task>` — team lead.
- `EXXXX <task>` — worker spawned by a team lead.
- `INDXXX <task>` — worker spawned directly by the parent.

Use the same `XXX` across implementation/review leads and their workers. Add a task suffix to distinguish concurrent workers. Prefix reflects who launched the agent, not task complexity. If a tool restricts identifiers, use lowercase technical IDs such as `tl_XXX_<suffix>`, `ex_XXX_<suffix>`, or `ind_XXX_<suffix>` and put the human label in the brief or ledger; do not assume tools support arbitrary display names.

## Slot budget

Default task budget is 16 slots; explicit task or repository limits may lower or raise it, within the current session's actual runtime ceiling. A config change alone does not prove capacity increased. Count the parent as one slot and every active lead and worker. A lead allocated N worker slots consumes N+1 global slots. Track reserved but not-yet-started capacity in planning without counting it as active; preserve headroom for required review teams when planning waves.

Before launch, the parent reserves capacity and states each package's worker cap explicitly. Leads stay within their allocation and cannot redistribute the global budget or expand their own cap. Preserve caps on continuation and correction. Finished agents free capacity only when runtime lifecycle confirms it; close them when supported. Do not reuse unrelated sessions to imply freed capacity.

If capacity is insufficient or a launch is rejected, report the needed slots to the parent and continue permitted coordination and acceptance work; do not repeatedly retry spawning. The parent reconciles observed capacity, queues the request without pausing the whole task or freezing other launches, reserves slots when available, then explicitly notifies the requester of the granted launch budget. Leads launch only within that cap. If independent work is exhausted, preserve a checkpoint and await the parent’s budget notification; capacity alone is not task failure or a blocked goal. If the project requires one implementation worker and two review workers, those teams cost two and three global slots respectively. Follow project or task-specific caps without imposing that workflow on unrelated tasks.

## Optional team leads

Delegate directly by default. Difficulty alone does not require a lead.
Use a fresh luna-max lead only for a small, parent-defined package that genuinely
needs coordinated workers, such as one bounded change with validation and fixes.

The lead decomposes locally, assigns bounded work to workers, manages dependencies
and corrections, inspects delivered changes and evidence, and reports one verified
outcome. Leads may directly make obvious, known, unassigned one-line fixes. Assign
substantial implementation, fixes, test execution, and documentation production to
workers. Never duplicate assigned work or use the exception as a fallback when slots
are lacking or a worker is blocked: request worker budget from the parent and await its
grant while continuing permitted coordination and acceptance work. Do not expand
scope, choose subsequent phases, or become a permanent role owner. Return oversized
packages for splitting. The parent retains project-wide decisions and final acceptance.

Select configured presets explicitly: luna-max for very complex work,
luna-xhigh for medium, luna-low for simple. Limit nesting to parent -> lead ->
workers. Workers execute; they do not spawn more agents. If nested spawning
is unavailable, the parent assigns the proposed workers directly.

## Execution

Apply base preset, fork_turns, fresh-session/reuse, and no-duplication rules at
every level. Include relevant rules and explicit role boundaries in each brief;
do not tell ordinary workers to act as orchestrators.

Independence includes branches/worktrees/index state, shared documents, DBs,
servers/ports, and heavy benchmarks. Isolate or serialize conflicting resources;
the parent decides conflicts across packages. Preserve foreign changes and
establish baseline/ownership when relevant; do not assume all dirty state was
preexisting.

Parallelize only independent work; never run competing edits to the same files.
Use notifications/wait tools, not status polling. Blocked agents save progress,
report the exact dependency, and end their turn; their supervisor owns continuation.
If work reveals a new independent subsystem, acceptance expansion, or a stall
requiring a scope decision, return saved progress and the exact dependency to the
parent; the parent splits or reprioritizes. Slot shortage follows the existing
request/grant protocol and does not freeze other launches.

Carry user corrections, permissions, confirmed product facts, and test or
communication cadence forward across compaction, restart, and agent replacement;
include them in remaining briefs. Explicit push authorization persists only
within the scope the user authorized. Do not invent production legacy data or
infer it from QA records. A requested per-slice completion notice and any
higher-priority communication rule override the generic quiet default.

Keep routine fixes within the assigned package. Escalate scope changes and
cross-package conflicts to the parent. Missing verification is unfinished work,
not evidence of success.

## Communication

Minimize internal messages in both directions at every level: parent <-> lead,
supervisor <-> worker, and peers. Put all known inputs, constraints, authority,
and acceptance criteria in the initial brief so execution needs no conversation.
Include this communication rule in every delegated brief.

After assignment, send only a decision or dependency the recipient must act on,
a material correction, an explicit request/grant required by this workflow, or
one final delivery with result, verification evidence, and unresolved issues.
Batch related questions and corrections into one compact message. Resolve routine
choices within assigned authority without asking. Escalate blocking decisions
promptly; silence must not hide a blocker or missing verification.

Do not send acknowledgements, thanks, restated instructions, intentions, progress
reports, repeated status requests, reminders, or confirmations of received reports.
Do not send a completion message and then repeat it in the final response; use
the automatic final notification as the delivery. Wait for notifications instead
of messaging to check activity. Contact peers only to resolve a concrete shared
dependency or conflict; no unsolicited advice, broadcasts, or relay chains.
Supervisors do not echo worker reports upward: consolidate evidence once when
their own package is ready, unless an earlier decision is required.

Only the parent addresses the user. Explain each spawn once in one short line:
`preset — bounded task — why this result is needed`. Batch related launches.
Leads send worker purposes to the parent as a batch to relay, not running updates.
Do not seek approval for routine launches unless it is actually required.

After launch explanations, stay quiet by default: no intermediate findings,
progress summaries, repeated plans, "still working", or individual completion
messages unless the user or a higher-priority rule requests them. Route necessary
questions through the parent and its available question tool. Otherwise, speak
only for issues requiring user attention, requested status, or the consolidated
final result with verification and unresolved issues.
