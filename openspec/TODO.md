# OpenSpec TODO

## `add-multi-user-manual-browser-workflow`

- [x] Complete implementation and offline verification slices 1–8. Immutable candidate `d20bf661a7fff42211dff0d62fe21c88746393e5`: 1049 passed, 24 browser tests deselected; release artifacts and installed distributions verified.
- [x] Hand off to a separate deployment agent. IND901 accepted the verified offline delivery; independent UI correction review passed six Chromium cases.
- [ ] Complete slice 9 on the selected ARM64/x86-64 hosts, in user-selected order: x86-64 first, then ARM64. Private x86 preflight and disposable staging started; initial browser attempts failed before readiness. Correct pinned fingerprint-model preload and full live acceptance remain pending. Keep actual host/access/configuration details outside Git.
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

- [x] Complete the uncapped `xhigh --fix` review, independent gap sweep and regression checks. All 51 original subagent reports recovered; reviewed source `2e42a93` passed 1110 tests with native fixtures and isolated Chromium, 24 real-engine cases deselected. Corrections and failed/superseding evidence are recorded in `completion.review`.
- [ ] During slice 9, audit ownership, permissions and traversable parent directories of migrated profile data. The supplied service uses `UMask=0077` for new files; existing data is not retroactively made private by migration.
- [ ] Resolve the deployment policy for authenticated import disk consumption, including staging copies, expanded data and concurrent requests. Streaming extraction bounds member memory, but does not impose a total archive or disk quota; supported large imports must remain consistent across API and proxy.
- [ ] Resolve whether the intended tenant trust model needs network egress isolation. Ordinary profile navigation currently retains host-reachable HTTP(S), including private destinations; no new URL-denial policy was selected in this review.
- [ ] If a future pinned VNC server advertises extended mouse buttons, verify their reachability through the bundled viewer and add matching device-aware disconnect cleanup before enabling that input path. The currently pinned path does not forward those buttons.
- [ ] Execute native Windows lock-contention and portable-platform CI checks for the revised candidate; mocked Windows primitive checks on Linux do not establish native Windows execution.
- [ ] Measure the retained single authorized fingerprint-stat scan per 120 ms desktop poll on the intended catalog sizes before selecting any further optimization; a lighter snapshot must preserve session, assignment and lifecycle reconciliation.
- [ ] Verify resolver-file ownership and modes during deployment preflight: the confinement contract accepts only root/service-owned resolver files without group/other write permission. Local namespace-fixture success does not establish that a target's resolver satisfies this contract.
- [ ] Before a future deployment, refresh retained distribution artifacts and the deployment handoff for the reviewed revision; the earlier d20bf66 artifact hashes remain historical and must not be assigned to the revised source.

- [x] Correct health-check URLs in both deployment guides and CLAUDE.md to `/api/v1/health` and `/api/v1/info`; actual x86 staging responses and source route decorators confirm the prefix.
- [ ] Pin and preload the supported fpgen model assets before protected browser launch; the first real x86 attempt exposed a model installation into the intentionally read-only dependency environment. Preserve read-only confinement and verify a real disposable launch after preload.
- [x] Expose the fixed `/bin` system alias in default protected runtime composition; native default-container regression reproduces missing `/bin/sh` before the fix and passes afterward. Real Camoufox x86 revalidation is still required.
- [ ] During the next pinned Camoufox dependency review, verify upstream VirtualDisplay startup-error formatting: Xvfb failure currently becomes an AttributeError after the process reference is cleared. Keep underlying operator diagnostics private; do not add a compatibility shim to the application.
- [x] Map the prepared GeoIP leaf read-only below each run's private writable XDG cache; preserve private fontconfig writes and prevent writes to the shared cache. Native regression reproduces the old EROFS failure and verifies readonly GeoIP plus private fontconfig after correction. Real x86 launch revalidation remains open.

- [ ] Correct and revalidate the protected supervisor control-header contract against the actual core controller; repeat disposable x86 navigation/snapshot/click/stop on a rebuilt immutable candidate before ARM testing. The prior candidate reached browser readiness but failed its first control action.
- [ ] Complete the 24 actual Chromium-engine pytest cases with a supported sandbox on x86-64. Root launch was refused; a disposable unprivileged retry reached the host user-namespace restriction. Investigate the pinned build's private setuid sandbox installation without disabling Chromium sandboxing or changing global host security; preserve explicit blocked outcomes if unavailable.
