# OpenSpec TODO

## `add-multi-user-manual-browser-workflow`

- [x] Complete implementation and offline verification slices 1–8. Immutable candidate `d20bf661a7fff42211dff0d62fe21c88746393e5`: 1049 passed, 24 browser tests deselected; release artifacts and installed distributions verified.
- [x] Hand off to a separate deployment agent. IND901 accepted the verified offline delivery; independent UI correction review passed six Chromium cases.
- [ ] Complete slice 9 on the actual ARM64/x86-64 hosts. Host identities/access, installation paths/revisions, native/browser pins, namespace/TLS configuration and staging resources remain unspecified; live acceptance is BLOCKED/NOT RUN.
- [ ] Keep live acceptance open and do not archive this change until required acceptance is complete.
- [x] Finish compatibility/startup gates, protected gateway composition, web/Flet integration, distribution verification and the offline deployment handoff.
- [x] Verify CI dependency installation and separate Linux native contracts from portable checks. Fresh-environment categories passed (163 passed, 18 opt-in skips); the native selection passed 89 without skips. Windows/macOS and Python 3.10 execution remains unverified locally.
- [x] Close the maintenance review findings: exclusive temporary files preserve the catalog and external bytes; descriptor-validated locking rejects planted links before chmod. Independent targeted review verified 113 passing tests within the trusted service-owned directory boundary.
- [x] Pin personal web/Flet actions, modals, Viewer and selection to immutable profile IDs. Explicit ID locators never fall back to names; canonical batch fields accept IDs only. Rename, deletion and UUID-as-name replacement regressions passed.
- [x] Deliver packaged staging account/profile fixtures, a controlled native-input page and explicit-target API smoke commands. Fifteen fixture/real-local-HTTP checks passed; actual target/browser acceptance remains NOT RUN.
- [x] Verify the private VNC backend with real Xvfb/x11vnc, including private RFB transport, helper watchdog, held-input release and browser-preserving cleanup; actual browser and live-host acceptance remain open.
- [x] Require authoritative RUNNING state before manual backend startup; verify RESERVED/STARTING/STOPPING requests create no helper and do not stop the browser.
- [x] Make fresh development timezone-alias tests reproducible with tzdata; preserve the existing probe algorithm and runtime dependency policy.

## Review follow-ups

- [x] Add package-required `reuse.md` with inspected source revisions, symbols, license, evidence, adaptations, and custom-work gaps; regenerate and verify `MANIFEST.sha256`.
- [x] Make MB-06/MB-07 and X05 require the selected gateway-only private VNC channel, deny untrusted-local direct access, and verify no backend credential reaches the client.
- [x] Set MB-11/N06 to a 60-second end-to-end cleanup maximum for tab closure, network loss, session expiry/timeout, and gateway failure, including detection and reconnect grace. The user selected this bound on 2026-11-05; a live helper or unconfirmed exit keeps the lease occupied and reports blocked recovery.
- [x] Restore the provided source-grounded OpenSpec skill byte-for-byte at `.agents/skills/source-grounded-openspec/SKILL.md`.
- [x] Make N10/S03 record display, browser-window, and page-visible geometry before/during/after noVNC, and check full-window fit on ARM64 and x86-64.
- [x] Install the complete supplied source-grounded skill archive, including its `agents/openai.yaml` metadata and `references/research-workflow.md`; confirm its `SKILL.md` is byte-identical to the earlier supplied document.
- [x] Close independent post-fix Stage 5 source-fidelity review: MB-11/N06 now cover tab closure, network loss, session expiry/timeout, and gateway failure under the 60-second bound, with fail-closed handling for an unconfirmed helper exit; see `completion.review`.
- [x] Close independent Stage 6 simplicity/reuse review by a reviewer who did not perform Stage 5, covering `reuse.md` and the changed MB-06/MB-07, MB-11, N06/N10/S03, and X05 criteria; see `completion.review`. Source tests were inspected but not run, and runtime/live acceptance remains NOT RUN.

- [x] Resolve the concurrent profile-transfer regression in `tests/test_transfer.py::test_import_extracts_normal_archive` before catalog-package acceptance.
- [x] Close catalog review follow-ups for overwrite recovery grants, pending profile-ID resolution, locked export/download authorization, and reentrant cross-process catalog locking; see `completion.review`.

## Fork review follow-ups (2026-11-07)

- [ ] Complete the uncapped `xhigh --fix` review, independent gap sweep and regression checks for the updated candidate; record every confirmed correction in `completion.review`.
- [ ] During slice 9, audit ownership, permissions and traversable parent directories of migrated profile data. The supplied service uses `UMask=0077` for new files; existing data is not retroactively made private by migration.
- [ ] Resolve the deployment policy for authenticated import disk consumption, including staging copies, expanded data and concurrent requests. Streaming extraction bounds member memory, but does not impose a total archive or disk quota; supported large imports must remain consistent across API and proxy.
- [ ] Resolve whether the intended tenant trust model needs network egress isolation. Ordinary profile navigation currently retains host-reachable HTTP(S), including private destinations; no new URL-denial policy was selected in this review.
- [ ] If a future pinned VNC server advertises extended mouse buttons, verify their reachability through the bundled viewer and add matching device-aware disconnect cleanup before enabling that input path. The currently pinned path does not forward those buttons.
- [ ] Execute native Windows lock-contention and portable-platform CI checks for the revised candidate; mocked Windows primitive checks on Linux do not establish native Windows execution.
- [ ] Measure the retained single authorized fingerprint-stat scan per 120 ms desktop poll on the intended catalog sizes before selecting any further optimization; a lighter snapshot must preserve session, assignment and lifecycle reconciliation.
