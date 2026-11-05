# OpenSpec TODO

## `add-multi-user-manual-browser-workflow`

- [ ] Complete the 40 implementation and offline verification tasks in slices 1–8 of `openspec/changes/add-multi-user-manual-browser-workflow/tasks.md`.
- [ ] Hand off slice 9 to a separate deployment agent; complete its five live deployment and acceptance tasks with recorded evidence.
- [ ] Keep live acceptance open and do not archive this change until required acceptance is complete.

## Review follow-ups

- [x] Add package-required `reuse.md` with inspected source revisions, symbols, license, evidence, adaptations, and custom-work gaps; regenerate and verify `MANIFEST.sha256`.
- [x] Make MB-06/MB-07 and X05 require the selected gateway-only private VNC channel, deny untrusted-local direct access, and verify no backend credential reaches the client.
- [x] Set MB-11/N06 to a 60-second end-to-end cleanup maximum for tab closure, network loss, session expiry/timeout, and gateway failure, including detection and reconnect grace. The user selected this bound on 2026-11-05; a live helper or unconfirmed exit keeps the lease occupied and reports blocked recovery.
- [x] Restore the provided source-grounded OpenSpec skill byte-for-byte at `.agents/skills/source-grounded-openspec/SKILL.md`.
- [x] Make N10/S03 record display, browser-window, and page-visible geometry before/during/after noVNC, and check full-window fit on ARM64 and x86-64.
- [x] Install the complete supplied source-grounded skill archive, including its `agents/openai.yaml` metadata and `references/research-workflow.md`; confirm its `SKILL.md` is byte-identical to the earlier supplied document.
- [x] Close independent post-fix Stage 5 source-fidelity review: MB-11/N06 now cover tab closure, network loss, session expiry/timeout, and gateway failure under the 60-second bound, with fail-closed handling for an unconfirmed helper exit; see `completion.review`.
- [x] Close independent Stage 6 simplicity/reuse review by a reviewer who did not perform Stage 5, covering `reuse.md` and the changed MB-06/MB-07, MB-11, N06/N10/S03, and X05 criteria; see `completion.review`. Source tests were inspected but not run, and runtime/live acceptance remains NOT RUN.
