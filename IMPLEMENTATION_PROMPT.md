# Prompt for the implementation agent

Implement the OpenSpec change `add-multi-user-manual-browser-workflow` in the user's fork of `chienbm98/foxprofile`.

Read the proposal, design, all seven capability specs, task checklist, source audit, API contract, acceptance plan and update runbook before changing code. Read the three bundled originals to resolve context. Preserve the user's required behavior and follow this exact architectural priority:

**Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY.**

Separate real domain policy, persistence and I/O boundaries. Use concrete DAOs/DTOs where useful; do not introduce ceremonial layers, universal policy engines, a new orchestrator, or speculative abstractions.

## Scope and stop boundary

You own **task slices 1–8 only**. Implement code and run offline/CI tests using disposable fixtures and development environments. Prepare deployment tooling, migration/backup/restore commands, runtime test harnesses and the handoff, but **do not deploy to, migrate, operate, or perform acceptance on the real servers**. The user explicitly reserved deployment/live acceptance for a **different agent in final slice 9**.

When slice 8 is complete, produce the concrete handoff and leave slice 9 unchecked with live results NOT RUN. Do not archive the OpenSpec change or claim production acceptance. This is a division of work, not a requirement to ask permission between routine implementation steps.

## Required implementation behavior

- Pin the real checkout and compare it with audited baseline `530b993ff3a7d211aaa0904fba0817b77c75d4c9`. Audit relevant differences before using a suggested hook. Do not infer installed production revision from this baseline.
- Keep new code/assets in a cohesive extension package and upstream edits small and explicit. Keep public API compatibility where compatible with personal access. Preserve existing profile catalog, portable format, browser directories and current UI.
- Use immutable local profile IDs plus sidecar extension data; avoid a catalog rewrite or name-based ownership. Handle interrupted rename/import/delete and silent save failures without exposing data.
- Enforce personal admin/user permissions at common service boundaries, not only buttons/routes. Service MCP remains separately trusted; never send its token to ordinary user code.
- Use one durable shared capacity admission path for every real browser launch, including helpers. Default 10, atomic batch reservations, stop/recovery correctness, no queue or user quota.
- Reuse upstream's working virtual-display and cleanup behavior. Preserve humanize and saved identity; no second browser/display to open noVNC.
- Implement noVNC inside the current Viewer with one temporary manual session per installation. Back only closes manual access and restores normal controls. No agent adapter, messages, CAPTCHA outcome check, Pause/Resume or global human/agent coordinator.
- Apply noVNC/runtime support to Camoufox on target Linux ARM64/x86-64; preserve ordinary Chrome behavior and explicit unsupported manual mode.
- Protect foreign profile and service files even through browser chrome/native noVNC input. Verify the narrow confinement adapter; REST URL validation alone is insufficient.

## Work organization

Execute dependency-ordered slices. Independent work after shared contracts are fixed may run in parallel. Give all delegates the same architecture priority, source constraints, upstream-maintenance rule and slice-9 boundary. Keep one owner per shared integration file; do not duplicate an unfinished task. Report concrete problems and decisions concisely.

Add focused tests for authorization, concurrency, lifecycle, migration and integration risks; use existing upstream tests for regression/update compatibility. Do not write tests that merely repeat implementation or broaden validation without a specific remaining risk. Record actual results and limitations.

The final implementation handoff must include the exact tested commit/artifact hashes, dependency/schema versions, changed upstream hook register, offline results, config and secret-delivery instructions without secrets, operational migration/backup/rollback tooling, and the complete [final-slice runbook](openspec/changes/add-multi-user-manual-browser-workflow/slices/09-live-deployment-and-acceptance.md).

