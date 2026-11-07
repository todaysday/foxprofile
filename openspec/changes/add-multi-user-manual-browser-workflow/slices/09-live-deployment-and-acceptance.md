# Slice 09 — Live deployment and acceptance

**Owner:** a deployment agent separate from the implementation agent.

**Position:** the final slice, after implementation slices/sections 1–8 and all offline/CI gates. The implementation agent may prepare scripts, fixtures, and this handoff; it does not perform this slice's live operations.

**Current status:** **INCOMPLETE.** The separate deployment agent completed bounded isolated x86-64 browser/noVNC/isolation/capacity and short 1/5/10-resource checks. Corrected checkpoint `a37cd81` passed actual Back/ordinary-control/cleanup revalidation and 1113 native-enabled non-browser tests. ARM followed x86 and completed bounded dedicated-UID noVNC, authorization and synchronized last-slot checks. x86 non-root confinement is blocked by host policy. Current review work is limited to fork changes; complete original package acceptance, production rollout and matched operational restore remain open.

## Objective and scope

Deploy the locked implementation of `add-multi-user-manual-browser-workflow`, prove the requested behavior on both existing target servers, and produce an evidence-based result. Targets are an ARM64 Linux instance and an x86-64 Linux instance, both without GPU. Each managed installation retains its own global browser limit, default 10, and at most one temporary manual VNC session. There is no new cross-server scheduler.

Preserve the existing FoxProfile manager and profile data. noVNC attaches to the existing Camoufox run; Back only closes manual access and restores ordinary panel control. Do not add agent messages, `/complete`, automatic CAPTCHA decisions, or Pause/Resume orchestration during deployment.

This assignment separation is explicitly required by the user. It is not a new permission or approval workflow. Missing target details, credentials, backups, or implementation evidence are concrete blockers to report, not values to invent.

## 1. Required handoff from the implementation agent

Before changing a real server, verify that the handoff contains:

- A clean, locked **fork commit/tag** to deploy and its exact upstream base. The research baseline is `530b993ff3a7d211aaa0904fba0817b77c75d4c9`; that is not a substitute for the eventual implementation commit. Never deploy floating upstream HEAD.
- Evidence for O01–O07 in [acceptance-plan.md](../references/acceptance-plan.md): exact commands/results, relevant upstream tests, extension tests, route/launch inventory gates, adapter contracts, and migration/restore fixture checks. Failed mandatory offline gates block live deployment.
- For initial release, the disposable baseline worktree and synthetic route/adapter drift checks are mandatory. A real-version upstream-update rehearsal is required only when a concrete additional candidate commit is selected; without one, U04 is N/A and does not block initial deployment. Do not invent evidence of a hypothetical merge.
- An extension/dependency manifest: schema version, tested Python/Camoufox/noVNC/VNC-adapter versions, supported architectures, finite core hooks, and activation procedure. New behavior should remain in cohesive extension modules with thin explicit hooks.
- Packaged install/configuration, migration, backup/restore, and health-check instructions with **actual implemented commands and paths**. Resolve the real systemd/container topology; do not assume that examples in the proposal are existing executable commands.
- A target-configuration **template** listing host/installation identity, access method, service/process owners, configuration/data/profile locations, reverse proxy/TLS, runtime socket namespace, and resource limits, populated only with values already supplied by the user. The implementation agent must not inspect live hosts to fill it. The deployment agent resolves the actual topology and required access during preflight, keeping credentials out of the report.
- The fixture/test runner for admin0/A/B/service, PA/PB, at least 11 disposable profiles, and the controlled drag/input page. Include test-only lifecycle/failure injection controls that cannot be left exposed publicly.
- A documented backup and matched rollback plan, required maintenance steps for consistent closed-profile copies, restore destination, and pre/post-deployment verification.
- A list of known limitations and untested conditions, including any update candidate not evaluated offline. The required design order is `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY`; deployment must not introduce hotpatches or a new general-purpose framework.

Offline handoff completeness concerns implementation-owned commits, templates, tooling, and evidence. Missing actual host addresses, credentials, or deployment topology block **slice 09 preflight only**; they do not block completion of offline slice 08 and do not authorize the implementer to discover live environments.

The deployment agent records its own identity/responsibility and the locked commit in the report. If a code defect requires a fix, return it to the implementation agent for a new commit and focused offline validation; do not patch the running production copy directly.

## 2. Read-only preflight and isolation

1. Inventory the actual revision, configuration, running profiles, process identities, browser/display topology, proxy arrangements, saved capacity setting, and service credential arrangement on each target. Record sensitive values only in the existing secret mechanism, never in logs or deliverables.
2. Compare the real checkout/runtime with the compatibility manifest. Verify the existing Camoufox `FOXPROFILE_HEADLESS=virtual` path and its runner-owned display metadata/cleanup are the tested ones. Do not layer another Xvfb or switch a running browser to obtain noVNC.
3. Check that the intended VNC adapter and noVNC assets are the tested supported builds for that architecture. The adapter remains replaceable; do not assume a similarly named binary has identical flags or maintenance status.
4. Resolve the X socket and Xauthority namespace. With systemd `PrivateTmp=true` or containers, the VNC child must access the exact display namespace of the selected browser, not merely an equal `DISPLAY` string.
5. Prepare an **isolated staging installation first**, with separate storage, profile directories, extension state, installation identity, listeners, and runtime processes. Do not point staging at production browser directories, run live migration against the only copy, or expose staging through unprotected public ports.
6. Keep target-specific deployment values in the configuration/manifest; do not edit browser engines or copied upstream panel files to make the stage work. Missing adapters or hooks must fail protected startup, never fall back to an unprotected panel.

## 3. Operational backup and test-copy migration

1. Follow the implementation's maintenance procedure to obtain a coherent backup: close/drain browsers as required for copying persistent profile directories, then back up application configuration/data, profiles, extension database/state, and the matching executable release. Preserve ownership/permissions and record restore instructions.
2. Do not kill active profiles merely because their current number exceeds a new limit. The default limit of 10 is admission policy, not a command to terminate working sessions. Test live-survivor recovery separately using disposable staging runs.
3. Restore the backup into the isolated staging location and verify it is usable. A backup file existing on disk is not a restore test. Test metadata/fingerprint/login preservation with appropriate controlled credentials; never publish sensitive values or overwrite the sole working sessions.
4. Install the locked fork release and its tested dependencies in staging. Run the additive extension migration using the actual packaged command. Bootstrap the first administrator through the allowed server-side mechanism; do not create a public self-promotion endpoint.
5. Confirm that existing/unassigned profiles belong to the administrator's visible scope, ordinary users see only explicit assignments, the trusted service credential remains server-side, an absent limit becomes 10, and an already saved valid limit is preserved.
6. Rerun migration and execute the controlled interrupted-migration/restore fixture on the staging copy. Pending imported/created profiles must not become public; stable profile identity, assignments, fingerprint/cookies, and service access must remain consistent.
7. Exercise matched rollback on a staging copy before production rollout. Restore code, configuration, and data together; do not run old unprotected code against the live extension-managed dataset as a shortcut.

## 4. Staging acceptance on both architectures

The authoritative executable case inventory is [acceptance-plan.md](../references/acceptance-plan.md). Preserve **NOT RUN** until each case executes; record evidence separately for each architecture where relevant.

### Access and capacity

- Execute **A01–A30**, covering all 30 rows of the access source: direct unauthenticated/foreign/admin endpoint denials, full control of assigned profiles, alternate identifiers, bulk all-or-nothing checks, live revocation, service separation, persistence, last-admin protection, and the shared limit.
- Use at least 11 test profiles for capacity. At nine occupied places, synchronize two launch requests to compete for the last place. Where supported, route them to different server workers. Inspect browser process creation and durable reservations; response codes alone are insufficient.
- Exercise existing helper-browser paths, including cookie tools, along with panel/API/MCP/background entry points. Preserve same-profile exclusivity and the difference between client waiting timeout and actual terminated startup.
- Inject a parent crash between runner spawn and durable PID/run registration in the disposable fixture. The runner must not create a browser before the parent-child binding/handshake commits; a delayed child after recovery or reservation reclamation must not create an unrecorded browser. Confirm occupancy and termination accounting through recovery.
- Verify default 10, invalid/user updates denied, saved 12 across restart, decrease to 6 without termination, all-or-nothing batch admission, failed-launch cleanup, duplicate/stale completion handling, and manager recovery with live browsers plus stale reservations.
- Keep injected crashes, failures, concurrent stops, and restore tests on disposable staging fixtures, not actual Pinterest work.

### Manual access and stealth

- Execute **N01–N13**, all 13 noVNC source rows, with the controlled input page and a supported Camoufox run. Verify exact run identity before/after, temporary VNC-only cleanup, ordinary input restoration, correct drag/keyboard/scroll behavior, released held keys/buttons, stable geometry, and unaffected neighbors.
- Execute the focused X-series boundaries: single manual-session races, duplicate/stale close, disconnect/reconnect, startup failure, gateway restart, reused displays/PIDs, arbitrary-target rejection, session revocation, Origin/CSRF, local-only VNC, and no secret leakage through URLs/logs.
- Execute **X08 / PA-14 with the actual host's UID/namespace behavior**. In the isolated staging copy, place harmless canaries in sibling-profile, service-config, extension-state, and another process's environment locations. Through noVNC Ctrl+L and native open/save dialogs, attempt those reads and a disposable write outside the permitted profile. They must be unavailable despite native input; an API allowlist is insufficient. Verify that the browser's own environment does not inherit service/session secrets, including through a `/proc/self/environ` exposure path. Exercise equivalent supported navigation/evaluation paths and confirm permitted browser behavior still works. Never print or use real secrets as canaries.
- Inspect source-case behavior with **no agent configured** as well as an independently waiting agent. Open/Back/disconnect/timeout must never create messages, completion events, success judgments, or pause/resume calls. The user tells the agent when ready outside FoxProfile.
- Verify Chrome's existing authorized management and capacity behavior; unsupported noVNC must explain the limitation without restarting or changing its engine.
- Execute **S01–S03 on each host**: proxy public egress and WebRTC check, persisted controlled login/fingerprint after restart, and same-run noVNC transitions with a successful ordinary click afterward.
- Verify new Camoufox profiles default to Linux and coherent saved geometry; existing identities remain unchanged. Keep native WebGL/Canvas and existing `humanize=True`. Do not add guessed GPU identities, JavaScript fingerprint overrides, or movement generators to mask a failed diagnostic.

### Load and resource measurements

On **each host**, measure identical recorded page/tab workloads at **1, 5, and 10 active browsers**, in three phases: no manual session, one connected noVNC, and after Back/cleanup. Record peak memory, CPU, manual-input responsiveness, browser/display/VNC process counts, and process accumulation after repeated transitions. Approximately 100 saved profiles should coexist independently of the active limit.

Do not assume the host can sustain ten browsers, that 16 GB is required, that 1366×768 guarantees a particular saving, or that ARM64/x86-64 fingerprints are identical. If a level cannot complete safely or responsively, record a failed/limited case and its evidence rather than claim the target passed.

## 5. Production rollout and live smoke checks

Only promote the **same locked commit and configuration class tested in staging**, after the required stage checks and restore path pass. Use the already authorized deployment context; this runbook adds no independent approval ceremony.

1. Apply the documented maintenance/backup sequence to the production instance. Preserve the matching prior release and data snapshot. Roll out one managed installation at a time so a failure does not obscure both targets.
2. Install the locked release and execute its packaged migration and activation steps. Keep access restricted until protected startup and authentication checks succeed. A missing extension hook or invalid schema is a stop condition, not a reason to fall back to the old single-token panel.
3. Confirm the live administrator/session flow, assigned-profile filtering, service credential separation, saved capacity, authoritative occupancy, and same-profile locking. Verify that public access uses HTTPS/WSS and that direct VNC listeners remain local-only.
4. Use a disposable profile for production smoke checks: ordinary start/Viewer, noVNC open/manual input/Back, next Playwright click, close cleanup, and a controlled denial for an unauthorized user. Confirm the real profile inventory and existing data were preserved without mass fingerprint changes.
5. Compare deployed versions and configuration with staging evidence and record any required environmental differences. Do not repeat disruptive crash/load tests against working profiles merely to obtain another pass; use the staging evidence for those cases and document the production smoke scope.
6. Repeat for the other architecture only after the first instance's rollout is stable. No distributed scheduler or cross-host quota is introduced.

## 6. Failure and rollback

Stop rollout on authorization bypass, untracked live browsers, wrong-display attachment, loss of profile data, failed protected startup, or a mandatory stage gate failure. Preserve redacted diagnostic evidence and the exact failing case.

If rollback is required:

1. Prevent new admission and external use of the affected deployment through the documented maintenance mechanism. Do not indiscriminately kill unrelated processes or neighboring installations.
2. Stop the failed release using its known owned process identities. Clean up only its owned temporary resources; never resolve ownership solely from a reused PID/display number.
3. Restore the **matched code/configuration/application+extension data/profile snapshot**. Do not guess a schema downgrade and do not expose an old authentication model as a fallback.
4. Restart through the documented restored configuration, reconcile actual browser occupancy, and verify authentication, profile ownership/data, service access, and ordinary controls before reopening access.
5. Record the rollback outcome, lost/retained transient sessions if any, failed evidence, and the implementation fix required. A rolled-back target is not reported as deployment passed.

## 7. Deliverable report

Produce a final `deployment-acceptance-report.md` with:

- Separate implementation-agent and deployment-agent responsibility records; locked fork/upstream commits, tested/deployed versions, hosts/architectures, and dates.
- Offline handoff evidence references and staging/production deployment outcomes for each target.
- Every A01–A30, N01–N13, S01–S03, focused-boundary, and mandatory update check marked `PASS`, `FAIL`, `BLOCKED`, or `NOT RUN`, with a concise evidence reference. U04 may be `N/A` only when no additional upstream candidate was selected. Both-host requirements need both-host evidence; missing required hardware is not N/A.
- The six-row, three-phase resource matrix populated with actual measurements, workload description, and temporary-process cleanup results. Include redacted raw metrics/log excerpts only when useful.
- Backup creation and test-restore evidence, migration/idempotency outcomes, deployed configuration differences, and rollback results or the verified standby restore procedure.
- Remaining limitations and a clear outcome for each target: deployed and accepted, deployed with explicitly unaccepted cases, blocked, or rolled back. Never infer a pass from a finished code task or valid OpenSpec package.

The report may link a redacted deployment manifest and metrics data generated by this final agent. Keep secrets, personal account data, site cookies, and signed access URLs out of all deliverables.
