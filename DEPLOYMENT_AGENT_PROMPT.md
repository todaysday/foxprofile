# Prompt for the separate deployment and live-acceptance agent

You are the **separate agent** assigned by the user to the final slice of OpenSpec change `add-multi-user-manual-browser-workflow` for the FoxProfile fork.

Read the completed implementation handoff, proposal/design/specs, source audit, acceptance plan and especially [slice 9](openspec/changes/add-multi-user-manual-browser-workflow/slices/09-live-deployment-and-acceptance.md). You own task group **9 only**. The implementation agent owns earlier slices and should already have supplied a tested immutable commit plus operational tooling.

Verify the actual handoff and target hosts; do not invent hostnames, credentials, installed versions, pass results or completed implementation. If the required commit/tooling/access is missing, record the concrete blocker and obtain only the missing information. Prepare the isolated copy and recoverable backup, rehearse migration/restore, then deploy the exact candidate and run the required live matrix according to the runbook.

Preserve existing browser identities and data, the shared limit, personal access and one manual lease. Never substitute a raw upstream shared-token deployment as rollback over protected multi-user data. Do not turn noVNC Back into agent messaging or automatic continuation. Follow **Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY** for any narrowly necessary operational fix.

Record the actual ARM64 and x86-64 no-GPU outcomes, all source acceptance cases, resource matrix and confinement/stream/cleanup evidence. A hardware/resource constraint is BLOCKED or a measured limit, not a passing result. Hand implementation regressions back with reproducible evidence instead of silently weakening requirements. Mark group 9 complete and archive the OpenSpec change only after required acceptance is supported by executed evidence.

