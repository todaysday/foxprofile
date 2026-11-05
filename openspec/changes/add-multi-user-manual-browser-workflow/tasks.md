# Implementation tasks and execution slices

**Change:** `add-multi-user-manual-browser-workflow`  
**Architecture priority:** Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY.  
**State:** implementation slice 1 is verified. Later implementation and live-acceptance tasks remain unchecked.

Code-level reuse provenance, exact source revisions/symbols, licenses, inspected test evidence, adaptations, and custom-work gaps are documented in [reuse.md](reuse.md).

## Responsibility boundary

`IMPLEMENTATION_AGENT` owns slices **1–8**. It may author deployment assets, migrations and test harnesses, and run offline/CI checks against disposable fixtures. It SHALL NOT deploy to, migrate, operate or accept the live servers. `DEPLOYMENT_AGENT` is a separate agent and owns **final slice 9**. Its runbook is [slices/09-live-deployment-and-acceptance.md](slices/09-live-deployment-and-acceptance.md).

After slice 8, stop at the completed handoff with slice 9 unchecked. Do not mark the whole feature production-ready or archive the change from offline evidence alone. Do not add an approval pause between routine local tasks; the separation here follows the user's explicit allocation of work.

Dependencies: 1 → 2 → 3; 2 → 4; 2 → 5; (3,4,5) → 6; (3,4,6) → 7; (1–7) → 8 → separate agent → 9. After slice 2, authorization, capacity and runtime implementation may run in parallel against agreed DTOs and adapters. Keep one owner for shared integration files; do not duplicate work or introduce team coordination infrastructure into the application.

## 1. Pin baseline and protect extension seams — IMPLEMENTATION_AGENT

- [x] 1.1 Compare the actual fork checkout with audited commit `530b993ff3a7d211aaa0904fba0817b77c75d4c9`; inventory existing specs, routes, direct Flet calls, launch/cookie-tool paths, serializers, tests and deployment assets. Verify with a recorded baseline and classified surface map in [implementation-baseline.md](references/implementation-baseline.md).
- [x] 1.2 Establish the isolated `foxprofile_ext` package/assets and narrow composition seams; record the reason for each upstream file edit. Verify the diff contains no copied upstream implementation or unrelated refactor. The public package factory binds only the assignment lookup; application adapters still own authentication and effects.
- [x] 1.3 Define principal/profile/runtime/error DTOs and policy/DAO responsibilities using existing patterns. Verify representative policy unit tests run without web server or browser dependencies.
- [x] 1.4 Prepare a compatibility test harness for auth/service boundaries, profile serialization and runner/process events; run the baseline upstream tests in the development checkout and record pre-existing failures separately. Current focused upstream and extension compatibility checks are documented in [implementation-baseline.md](references/implementation-baseline.md).

## 2. Add stable identity and durable extension state — IMPLEMENTATION_AGENT

- [ ] 2.1 Add immutable local profile IDs through minimal model/load/save hooks; preserve on rename/authorized overwrite and omit from portable ZIPs. Verify round-trip, repeated migration, fresh-import and forged-ID cases with fixtures.
- [ ] 2.2 Implement local SQLite DAOs/migrations for users, sessions, grants, settings, reservations/manual leases and necessary catalog-operation recovery. Verify atomic writes, constraints, permissions and idempotent schema upgrades on disposable databases.
- [ ] 2.3 Wrap catalog create/import/rename/delete with shared mutation locking, cache reload, finite operation intent and recovery. Surface save failures and quarantine residual deleted data. Verify failure injection at filesystem/catalog/ACL boundaries cannot expose another profile's cookies or lose grants.
- [ ] 2.4 Implement bootstrap and last-active-admin protection; preserve legacy browser data and default capacity to 10 only when absent. Verify repeated bootstrap/migration, concurrent admin changes and preserved valid settings on copied fixtures.
- [ ] 2.5 Provide scoped upload/download staging and safe portable export/import adapters without arbitrary ordinary-user server paths. Verify archive path/authority injection, overwrite authorization and export contents.

## 3. Enforce identity and full-profile authorization — IMPLEMENTATION_AGENT

- [ ] 3.1 Implement login, logout, password hashing/reset and revocable personal sessions with protected cookies/CSRF/Origin checks; preserve distinct service-only MCP credentials. Verify user sessions cannot inherit or receive the service token.
- [ ] 3.2 Apply common policies to every classified REST, direct-service, Flet, file, screenshot, tab/browser/viewer and global operation. Verify admin/A/B/service matrices through direct calls, not only UI visibility.
- [ ] 3.3 Filter lists/search/counts/groups/logs/proxy views server-side and resolve alternate resource IDs to profiles. Verify no foreign metadata or secrets appear in responses or exports.
- [ ] 3.4 Implement atomic create-and-assign, admin grants/revocation and disabled-user handling while retaining all allowed profile settings/transfer/delete controls. Verify tampered role/owner fields and mixed-authority batches fail before effects.
- [ ] 3.5 Propagate revocation to active Viewer/manual connections and subsequent commands without stopping browsers. Verify logout, disable, role change and assignment revocation on integration fixtures.

## 4. Implement shared profile capacity — IMPLEMENTATION_AGENT

- [ ] 4.1 Add the persistent validated installation setting and shared atomic reservation DAO/service; preserve profile exclusivity. Verify default 10, min 1, administrator control, and two-process competition for the last place.
- [ ] 4.2 Integrate every normal, background, MCP and browser-backed helper launch before spawn. Verify full-capacity rejection produces no process and no administrator/service bypass.
- [ ] 4.3 Add true all-target batch preflight/admission and route existing web/Flet bulk starts through it. Verify insufficient capacity or one forbidden target starts none, duplicate targets do not double-count, and post-admission failures report actual per-target outcomes.
- [ ] 4.4 Bind reservations to unique run IDs and verified process identities; register/claim the child before it creates a browser and keep stopping/failed-but-live runs counted. Verify parent death in the spawn/registration gap, late cancelled child, repeated/stale exit callbacks, partial spawn and HTTP wait timeout with controlled processes.
- [ ] 4.5 Implement restart reconciliation and live limit changes. Verify stale-versus-live records, reused PIDs, fail-closed uncertainty and lowering below occupancy without forced termination.

## 5. Extend existing runtime and preserve stealth — IMPLEMENTATION_AGENT

- [ ] 5.1 Extend existing runner/display registration with host/profile/run identity, actual display and private X context; keep CONTROL readiness and current Xvfb cleanup. Verify exact binding, stale generation rejection and no nested display creation.
- [ ] 5.2 Retain configured virtual mode and `humanize=True`; share only necessary launch-policy code with cookie helpers. Verify no hardcoded true-headless helper bypass and no new support for operations upstream requires stopped.
- [ ] 5.3 Set Linux defaults only for new Camoufox profiles; preserve saved fingerprint/cookies/proxy/locale and geometry. Verify fixture round-trips and no automatic changes on login/assignment/noVNC transitions.
- [ ] 5.4 Implement and test the minimum per-profile process/filesystem confinement needed for full noVNC browser control, using a narrow spawn adapter and sanitized child environment. Verify sibling data, service secrets and other-process environment are inaccessible in an isolated development harness; do not treat REST URL filtering as proof.
- [ ] 5.5 Preserve other engines' permitted operations and shared capacity; expose explicit unsupported manual-runtime behavior. Verify Chrome/legacy true-headless requests do not restart, switch engine or create a replacement browser.

## 6. Add temporary manual sessions and WebSocket gateway — IMPLEMENTATION_AGENT

- [ ] 6.1 Implement one installation-wide manual lease, scoped to personal session/profile/host/run and held during provisioning/closing. Verify double-click and concurrent open behavior creates at most one adapter.
- [ ] 6.2 Implement replaceable x11vnc adapter plus authenticated same-origin WebSocket bridge and a gateway-only private connected backend channel. Verify untrusted local processes cannot connect directly, no backend credential reaches the client, and no public/arbitrary target, global X11 relaxation, or shell injection is possible.
- [ ] 6.3 Implement status, reconnect and idempotent close with authorization/run checks and a maximum 60-second end-to-end cleanup deadline for tab closure, network loss, session expiry/timeout, and gateway failure, including detection and any reconnect grace. Verify stale display reuse, repeated close, child failure and logout/revocation never affect a replacement or neighboring run; an unconfirmed live helper keeps the lease occupied and reports blocked recovery.
- [ ] 6.4 Release held keys/buttons during graceful/forced cleanup and preserve browser/display lifetime and capacity. Verify next ordinary control action in a disposable integration harness is clean.
- [ ] 6.5 Verify by code and integration tests that no create/close/error path sends an agent message, checks CAPTCHA outcome, controls an agent task or adds a Pause/Resume service.

## 7. Integrate the existing panel — IMPLEMENTATION_AGENT

- [ ] 7.1 Add personal entry/current-user UI and minimal administrator user/assignment/settings controls in the existing panel. Verify permitted user controls remain complete and global sections are administrator-only.
- [ ] 7.2 Show administrator effective occupancy and capacity editing plus redacted capacity errors for users. Verify validation, persistence and lower-than-occupancy explanation in local UI tests.
- [ ] 7.3 Place «Открыть в noVNC» above the current Viewer and embed noVNC there with local scaling and remote resizing disabled. Verify no new tab/profile selection and no duplicate local REST input/polling.
- [ ] 7.4 Implement «Вернуться назад» and error states restoring a fresh normal Viewer and its handlers. Verify safe repeated actions, generic foreign-profile busy messages and no automatic agent continuation.
- [ ] 7.5 Keep new UI logic/assets separate and existing panel edits small; verify local browser navigation and API contract smoke checks against the extension without replacing the upstream UI.

## 8. Finish offline verification and deployment handoff — IMPLEMENTATION_AGENT

- [ ] 8.1 Run focused domain/DAO/API/process/UI tests, all source-scenario offline equivalents, supported two-process races and existing upstream regression tests. Record actual results and pre-existing failures; do not claim live scenarios passed.
- [ ] 8.2 Add compatibility checks for changed/new protected routes, direct-service paths, serializers, readiness/cleanup events, helper launches and package startup. Verify a deliberately unclassified test surface or missing required hook fails the gate.
- [ ] 8.3 Prepare deployment assets for virtual mode, confinement, same service/X namespace, WSS, pinned client/VNC dependencies and backup/migration/rollback commands. Validate syntax and packaging locally; do not apply to live hosts.
- [ ] 8.4 Prepare the controlled upstream-update workflow on a disposable checkout, with finite hook register and extension tests separated from upstream source. Verify the workflow can identify a simulated adapter incompatibility without modifying a live deployment.
- [ ] 8.5 Produce the exact tested fork commit, artifact/dependency/schema hashes, compatibility manifest, config example, offline test report, known limitations and final-agent instructions. Verify every implementation-owned input in slice 9 is supplied without credentials; label actual host/access details for the deployment agent's preflight when not already provided.
- [ ] 8.6 Hand off to a separate deployment agent with task group 9 still unchecked. Verify the handoff distinguishes implemented/offline-verified, live NOT RUN, and any BLOCKED acceptance; do not archive the change here.

## 9. FINAL SLICE — deploy and accept on live hosts — DEPLOYMENT_AGENT ONLY

- [ ] 9.1 Consume and verify the immutable handoff, actual target/installed revisions and architecture; prepare the isolated test copy and rollback point according to the separate runbook. Record checks against the delivered artifacts.
- [ ] 9.2 Take consistent operational backups, rehearse migration/restore on the isolated copy, then deploy the tested candidate/configuration incrementally. Record the deployed commit, schema/dependencies and actual service/WSS/confinement state.
- [ ] 9.3 Execute all 30 user-access/capacity, 13 noVNC and 3 stealth source acceptance cases plus the concrete cross-feature/update/isolation checks on the real target environments. Record PASS/FAIL/BLOCKED/NOT RUN with evidence and preserve live data.
- [ ] 9.4 Measure the 1/5/10-browser matrix before/during/after one manual session on ARM64 and x86-64 without GPU; record workload, memory, CPU, input response and process cleanup. Treat resource limits as measured constraints, not a passing ten-browser claim.
- [ ] 9.5 Exercise the documented rollback or restore drill on the safe copy, resolve or hand back failed acceptance to the implementation agent, and produce the final deployment/acceptance report. Mark this slice complete only from executed evidence; archive OpenSpec only when all required acceptance is satisfied.

