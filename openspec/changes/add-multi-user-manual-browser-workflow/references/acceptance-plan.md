# Implementation acceptance plan

**Mandatory acceptance status: NOT RUN.** This package specifies work; it does not contain an implemented fork or claim successful browser, migration, security, concurrency, or performance tests. Mandatory implementation checks below are **NOT RUN**. U04 is a conditional real-upstream-update rehearsal and is **N/A for an initial release without a selected update candidate**; synthetic drift checks remain mandatory. Document-format validation is separate from product acceptance.

Source labels used in the tables:

- **Access:** [FoxProfile_User_Access_Spec.md](inputs/FoxProfile_User_Access_Spec.md), v0.2; its 30 acceptance rows are in §10.
- **noVNC:** [FoxProfile_noVNC_Manual_Handoff.md](inputs/FoxProfile_noVNC_Manual_Handoff.md), v1.1; its 13 acceptance rows are in §12.
- **Stealth:** [FoxProfile_Stealth_Configuration.md](inputs/FoxProfile_Stealth_Configuration.md); its three numbered host checks are in §4.
- **Conversation:** the user's additional requirements for upstream-update resilience and `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY`.

## 1. Fixture and evidence contract

The user requires **two separate agents and a final deployment slice**. The implementation agent owns sections/slices 1–8: product code, test fixtures, offline/CI tests, migration and deployment scripts, and documentation. That agent **does not deploy to, migrate, or run acceptance against the real servers**. A different deployment agent owns slice 09, [Live deployment and acceptance](../slices/09-live-deployment-and-acceptance.md), after implementation and offline gates finish. It owns operational backups, staging and production deployment, live migration, real browser/proxy/display tests, load measurements, rollback, and the final acceptance report.

Preparing or executing a case in an isolated offline/CI harness does not mark its final live-acceptance row as passed. The final tables in §§3–6 and §8 are executed and signed off by the deployment agent; §7 supplies offline evidence from the implementer plus the deployment agent's final verification of the selected release. This separation is an explicit user requirement, not an additional approval step.

Use an isolated test installation and synthetic profiles. In slice 09, before migrating any copied real data, the deployment agent takes a recoverable backup of application configuration/data, extension data, and **closed** browser-profile directories, retaining permissions. Restore that backup into a test location first. Do not use the only copies of working Pinterest sessions. Keep code revision, configuration, schema version, and data snapshot matched for restore testing.

Prepare the following fixture:

| Fixture | Required state |
| --- | --- |
| `admin0` | Active administrator created through the permitted server bootstrap mechanism. |
| `A`, `B` | Separate active ordinary users with distinct credentials and sessions. |
| Trusted service principal | Existing separately configured MCP/API credential; it is not a third human role and is never placed in A/B's browser. |
| `PA`, `PB` | Disposable profiles assigned only to A and B respectively, with recognizable dummy metadata and separate persistent directories. |
| Capacity profiles | At least 11 disposable profiles total; preferably 12 so the limit-of-12 check can exercise the saved setting. Distribute assignments between A/B and retain an unassigned profile accessible to the administrator/trusted service and to no ordinary user. |
| Storage/load fixture | Approximately 100 saved profiles; 1, 5, and 10 selected for controlled workload runs. Saving profiles alone must not consume browser capacity. |
| Manual-input page | Controlled page with a draggable element, text field, scroll area, and visible pointer/button-state feedback. Avoid depending on a real CAPTCHA for acceptance. |
| Native-browser isolation fixture | Harmless unique canary files in PA, PB, service config, and extension state; a separate same-service-UID test process with a canary environment variable; a service-secret canary that must be excluded from the browser's own environment; disposable write targets outside PA. Use staging/CI copies, never real secrets. |
| Proxy/session fixture | Known test proxy, known host public IP, and a controlled login/cookie marker for persistence checks. Never publish proxy credentials or real site cookies in evidence. |
| Hosts | One ARM64 Linux host and one x86-64 Linux host, both without GPU. Record actual versions and resource limits. |
| Other engine | One supported existing Chrome profile for access/capacity regressions and explicit unsupported-noVNC behavior. |

Record the actual upstream and fork commits, extension schema version, Python/Camoufox/noVNC/VNC-adapter versions, OS/architecture, process-worker topology, test command, exit code, and redacted relevant evidence. The researched baseline is `530b993ff3a7d211aaa0904fba0817b77c75d4c9`; a different implementation checkout requires a bounded re-audit of the integration surface.

For each mandatory case replace **NOT RUN** only after execution with `PASS`, `FAIL`, or a precise `BLOCKED` reason plus an evidence reference. Missing required hardware is `BLOCKED`, not an inferred pass or N/A. U04 alone may be N/A when no additional upstream revision is selected. Never record a theoretical expectation as an observed result.

## 2. Execution groups

### 2.1. Offline/CI work — implementation agent, slices 1–8

| Offline gate | Scope and required evidence | Result |
| --- | --- | --- |
| O01 — Domain rules | Authorization decisions, last-admin invariants, capacity reservation/release, and migration consistency without a web server or real browser. | NOT RUN |
| O02 — DAO/process concurrency | Actual shared-storage integration in isolated local/CI fixtures for transactional and cross-process claims; synthetic profile data only. | NOT RUN |
| O03 — API/gateway contracts | Anonymous/A/B/admin/service requests against the test harness; alternate IDs, file boundaries, long-lived stream authorization, route inventory, and all launch adapters. | NOT RUN |
| O04 — Runtime/manual/isolation harness | Focused runner/adapter/readiness tests, deterministic lifecycle/failure races, and a per-profile filesystem/process-confinement fixture using isolated CI resources. Package the controlled input page and live runner; no real-server execution. | NOT RUN |
| O05 — Migration/restore tooling | Idempotent and interrupted migration tests on disposable synthetic or supplied test-copy data; produce operational backup/restore scripts and instructions without accessing live data. | NOT RUN |
| O06 — Upstream compatibility | §7 contract/diff/activation gates, required existing upstream tests, a disposable worktree of the locked baseline, and synthetic route/adapter drift tests are mandatory. Rehearse a real-version upstream merge only if an explicit candidate is selected (U04). No server deployment. | NOT RUN |
| O07 — Handoff | Locked implementation commit, exact test commands/results, dependency/adapter manifest, deployment configuration template, source-case runner, restore procedure, and outstanding limitations. | NOT RUN |

### 2.2. Live work — separate deployment agent, final slice 09 only

1. **Server and gateway acceptance:** use direct requests with anonymous, A, B, admin0, and service credentials; probe alternate object identifiers, exports/imports, long-lived Viewer/WebSocket access, and all recorded launch paths. UI hiding is not evidence of authorization.
2. **Focused real-browser acceptance:** run the controlled page, real launch/stop and helper-browser paths, failure injection, same-run noVNC lifecycle, and process/display cleanup on isolated staging first.
3. **Manual UI and host acceptance:** check the existing panel, role-specific visibility, ordinary Viewer/noVNC transitions, input, stable geometry, real proxy egress, and the measured host matrix below.
4. **Operational migration and deployment:** back up and restore a test copy, deploy only the locked release, run live migrations using the packaged procedure, verify production smoke checks, and execute or document rollback as needed.
5. **Final report:** record results for every source case and each target architecture, preserving NOT RUN/BLOCKED for anything not executed. Do not modify product code directly on the real server to evade a failing gate; return fixes to the implementer for a new tested commit.

Use barriers or controlled synchronization for race tests; avoid sleep-based claims that requests were concurrent. Where multiple server processes are supported, competing requests must reach different processes sharing one installation. If production is deliberately single-manager, test its admission owner across clients and test that an unsupported second manager cannot start independently; do not claim multi-worker coverage from one process.

## 3. User-access source acceptance

All 30 source rows are preserved. API requests use the actual route inventory from the pinned fork; proposed endpoint names must not be assumed to exist before implementation. The implementation agent prepares automation/offline coverage; final staging/live results below belong to the separate deployment agent in slice 09.

| Case / source | Executable check and required assertion | Requirements | Execution | Result |
| --- | --- | --- | --- | --- |
| A01 — Access §10/#1 | Request the panel, protected API data, and a Viewer without valid authorization; all protected content is denied. Repeat with expired/revoked credentials. | UA-03, PA-04, PA-05 | Direct requests + UI | NOT RUN |
| A02 — Access §10/#2 | Log in as A; inspect list, search, running list, filters, groups, counters, and related responses. PA is visible; PB and its metadata are absent from responses, not merely hidden by JavaScript. | PA-06, UA-07 | Direct requests + UI | NOT RUN |
| A03 — Access §10/#3 | As A, start/stop PA and open its Viewer; permitted operations work under the existing browser-state rules. | PA-03, PA-04, GC-03 | Server/browser + UI | NOT RUN |
| A04 — Access §10/#4 | Have admin0 and separately the service agent start PA; A opens Viewer. Compare run/process/tab identity before/after: no second browser or occupancy is created. | PA-13, GC-02, VR-06 | Browser integration | NOT RUN |
| A05 — Access §10/#5 | As A, edit PA's supported settings, proxy, and fingerprint using the existing stopped-browser requirements; reload/relaunch and verify the explicit changes persist. PB is unchanged. | PA-03, PA-10, ST-02, ST-03 | Server/browser + UI | NOT RUN |
| A06 — Access §10/#6 | As A, import/export controlled PA cookie data through each existing path; exported contents belong only to PA. A stopped-profile helper obeys exclusivity and capacity. | PA-03, PA-09, GC-03 | API/file + browser integration | NOT RUN |
| A07 — Access §10/#7 | As A, delete a disposable assigned profile through the existing supported operation; its own data is removed as specified, PB and service data remain intact. | PA-03, PA-12, GC-08 | Server/file integration | NOT RUN |
| A08 — Access §10/#8 | Substitute PB's stable identifier in every enumerated read/edit/start/stop/delete/export/cookie/proxy/fingerprint route as A; each denies access before reading protected contents or creating side effects. | PA-04, PA-05, PA-09, PA-10 | Parameterized direct requests | NOT RUN |
| A09 — Access §10/#9 | As A, substitute a PB tab/browser/screenshot/Viewer/export identifier, including a mismatched PA profile field; the server resolves ownership and denies access without returning pixels or files. | PA-05, MB-05 | API/stream integration | NOT RUN |
| A10 — Access §10/#10 | As A, request each administrative route/API directly, including users, assignments, settings, MCP configuration, and service keys; all deny independently of UI state. | UA-04, UA-07, PA-11 | Direct requests + UI | NOT RUN |
| A11 — Access §10/#11 | Modify frontend role state and inject role/owner/assignment fields into user requests, profile creation, and import metadata; neither privileges nor assignments change. | UA-01, PA-02, PA-10, PA-11 | Parameterized direct requests | NOT RUN |
| A12 — Access §10/#12 | As A, submit each existing bulk mutation with both PA and PB; inspect data/processes to verify rejection before any target changes. | PA-07, GC-05 | Server integration | NOT RUN |
| A13 — Access §10/#13 | With A's Viewer/stream open, admin0 revokes PA's assignment; the next API/input command is denied and protected image transmission stops. The browser remains alive for authorized principals. | PA-08, MB-05 | Live API/stream integration | NOT RUN |
| A14 — Access §10/#14 | With A logged in and viewing a profile, admin0 disables A; all A sessions/streams lose access while profile data and admin/service access persist. | UA-03, UA-04, PA-08, MB-05 | Live API/stream integration | NOT RUN |
| A15 — Access §10/#15 | As A, create/import and, where natively supported, clone a profile; the committed profile receives a fresh local identity and A's grant, B never sees it, and imported ownership cannot override that assignment. | PA-02, PA-09, PA-12 | Server/file + UI | NOT RUN |
| A16 — Access §10/#16 | As admin0, manage PA/PB, assign/revoke profiles, create/reset/disable users, choose admin/user roles, and edit allowed global settings; operations succeed without bypassing profile capacity. | UA-04, UA-07, PA-03, PA-11, GC-03, GC-12 | Server + UI | NOT RUN |
| A17 — Access §10/#17 | Restart the service and repeat login; users/assignments/settings survive and persisted browser data remains intact. Exercise the supported host-restart path in staging, not on the only working sessions. | UA-09, PA-12, ST-02, UC-06 | Staging restart + browser | NOT RUN |
| A18 — Access §10/#18 | Inspect A's HTML/JS config, API responses, storage, logs visible to A, and profile/cookie exports; no shared service key, user database, PB data, or unrelated system files appear. | UA-02, UA-06, UA-07, PA-06, PA-09 | Response/export inspection | NOT RUN |
| A19 — Access §10/#19 | Use the separately configured trusted MCP/API service credential to access intended profiles after migration; existing automation works and every browser-creating path still obeys shared capacity. | UA-06, GC-03, VR-06 | MCP/API integration | NOT RUN |
| A20 — Access §10/#20 | Attempt to disable/demote the last active admin; deny it. Also race two administrator-removal operations in shared storage; at least one active administrator remains. | UA-05 | Server/DAO integration | NOT RUN |
| A21 — Access §10/#21 | Initialize/migrate without a saved limit; effective capacity is 10. Store at least 100 stopped test profiles; no active capacity is consumed by storage alone. | GC-01, GC-02 | Migration/storage + API | NOT RUN |
| A22 — Access §10/#22 | Fill ten places using mixed panel/API/MCP paths, including starting reservations. Request another launch through each supported entry point and admin/service authority; all reject before spawning a browser. | GC-02, GC-03, GC-11 | Browser/process integration | NOT RUN |
| A23 — Access §10/#23 | Hold nine occupied places; use a barrier to submit two different authorized launches simultaneously, across workers where supported. Exactly one gains the final place; verify total actual occupancy never exceeds ten. | GC-04 | Deterministic concurrent integration | NOT RUN |
| A24 — Access §10/#24 | At ten running/starting places, A opens PA's Viewer repeatedly and performs allowed manual input; occupancy stays unchanged. noVNC attachment is governed only by its separate manual-session availability. | GC-02, GC-11, MB-02 | Browser integration + UI | NOT RUN |
| A25 — Access §10/#25 | At capacity, test normal stop, verified crash, startup failure, cancellation, and actual timeout cleanup separately. Places free only after process termination; duplicate cleanup is harmless and the next launch succeeds. | GC-06, GC-08 | Staging failure injection | NOT RUN |
| A26 — Access §10/#26 | As A, attempt a direct capacity update; deny it. As admin0 submit 0, negative, fractional, boolean, and malformed values; reject each and retain the prior setting. | GC-01 | Parameterized direct requests | NOT RUN |
| A27 — Access §10/#27 | As admin0 save 12, restart the service, and verify both persisted settings and all launch paths use 12; rerun migration without resetting the valid value. | GC-01, GC-03 | Restart/migration + API | NOT RUN |
| A28 — Access §10/#28 | With ten active profiles, save 6; all existing runs remain alive and new launches stay blocked until occupancy is below six. Confirm the administrator warning and subsequent admission. | GC-10, GC-12 | Browser integration + UI | NOT RUN |
| A29 — Access §10/#29 | With nine places occupied, submit a bulk launch of two new allowed profiles; reject the whole batch before either starts and leave no reserved place behind. | GC-05 | Concurrent server/process integration | NOT RUN |
| A30 — Access §10/#30 | Restart the manager with verified live browser sessions and abandoned pre-spawn reservations. Before any new admission, reconcile real process/run identity, count survivors, remove only proven stale reservations, and reject reused-PID misidentification. | GC-09, VR-02 | Staging recovery injection | NOT RUN |

## 4. noVNC source acceptance: all 13 rows

| Case / source | Executable check and required assertion | Requirements | Execution | Result |
| --- | --- | --- | --- | --- |
| N01 — noVNC §12/#1 | From PA's existing ordinary control screen, select "Open in noVNC"; the same profile appears inside that screen without a new tab or a second selection. | MB-01 | Browser integration + UI | NOT RUN |
| N02 — noVNC §12/#2 | Record PA's `browser_run_id`, verified process identity, tabs, and display; open and close noVNC; those identities remain unchanged and no extra browser is created. | MB-02, VR-01, VR-02, VR-04 | Browser integration | NOT RUN |
| N03 — noVNC §12/#3 | On the controlled page, press, hold, move, and release a draggable element; verify continuous drag, typing, and scrolling through the VNC path. | MB-08 | Browser integration + UI | NOT RUN |
| N04 — noVNC §12/#4 | Select Back; VNC input/connection is closed, temporary VNC resources terminate, a fresh ordinary image appears, and existing panel controls work once. Camoufox/Xvfb remain running. | MB-09, MB-10, VR-04 | Browser integration + UI | NOT RUN |
| N05 — noVNC §12/#5 | Perform open/Back with no agent configured, then while a separately waiting agent exists. Behavior is identical; no completion message, CAPTCHA decision, pause, or resume callback is emitted. | MB-12 | Integration + contract review | NOT RUN |
| N06 — noVNC §12/#6 | During active input, exercise each trigger separately: close the client tab, drop the network, expire/timeout the session, and fail/restart the gateway. Start one end-to-end timer at each trigger; verify access is revoked and detection, any reconnect grace, input release, VNC-helper termination and confirmed exit all complete within 60 seconds. No agent action is generated. If helper exit cannot be confirmed, within 60 seconds verify a blocked/recovery error, an occupied lease, and denial of a conflicting manual session. | MB-11, MB-12 | Browser/process integration with trigger and termination-failure injection | NOT RUN |
| N07 — noVNC §12/#7 | Close session S1 twice; after opening S2, replay S1's close; no error causes leaked processes and S2 is unaffected. Also close while S1 is still starting; late readiness cannot reopen it. | MB-03, MB-10 | Integration with controlled races | NOT RUN |
| N08 — noVNC §12/#8 | Keep other profiles active while PA opens/closes/fails its manual session; neighbors retain their run IDs and authorized API/MCP control. | MB-13, VR-04, VR-05 | Browser integration | NOT RUN |
| N09 — noVNC §12/#9 | End PA's run, reuse its numeric display for a replacement run, and replay old WebSocket/reconnect/close requests; they cannot attach to or close the replacement. Repeat with equal display strings in separate host/container namespaces. | MB-04, MB-05, VR-02, VR-05 | Integration | NOT RUN |
| N10 — noVNC §12/#10 | On ARM64 and x86-64, record before/during/after noVNC the X display framebuffer width/height; browser outer-window x/y/width/height; and page `screen.width`, `screen.height`, `screen.availWidth`, `screen.availHeight`, `innerWidth`, `innerHeight`, and `devicePixelRatio`. Open/close noVNC and resize/rotate its client viewport. Verify the full browser window fits within the display and every remote value remains unchanged; only local image scaling changes. | MB-08, ST-02, ST-03, ST-06 | Per-host browser integration + UI | NOT RUN |
| N11 — noVNC §12/#11 | Close/drop VNC during a held key/button, then perform the next ordinary Playwright click; no stuck modifier or drag state remains and the existing coordinate-click path is preserved. | MB-10, MB-11, ST-04 | Browser integration | NOT RUN |
| N12 — noVNC §12/#12 | Close a session and replay its access material; try another user's profile/session and revoke an active user's assignment; access/pixels/input stop and temporary resources are reclaimed without stopping the browser. | MB-04, MB-05, MB-06, MB-07 | Server + WebSocket integration | NOT RUN |
| N13 — noVNC §12/#13 | Repeat the complete supported workflow on both target architectures without GPU and record the 1/5/10 browser measurements. Distinguish tested limits from assumptions. | MB-14, VR-07, VR-08, ST-01, ST-07 | Host acceptance | NOT RUN |

## 5. Stealth source acceptance: all three host checks

Run each row on **both** target hosts; the combined case passes only when evidence exists for both.

| Case / source | Executable check and required assertion | Requirements | Execution | Result |
| --- | --- | --- | --- | --- |
| S01 — Stealth §4/#1 | Inside the representative proxied profile, inspect `https://browserleaks.com/ip` and `https://browserleaks.com/webrtc` or an equivalent controlled diagnostic; record effective public egress and confirm the server's public IP is not exposed by the tested WebRTC path. Redact credentials. | ST-03, ST-07 | Per-host browser check | NOT RUN |
| S02 — Stealth §4/#2 | Record a controlled login/cookie marker and saved fingerprint; stop and relaunch the profile; login data and saved fingerprint persist in the same profile directory. | ST-02, VR-06 | Per-host browser check | NOT RUN |
| S03 — Stealth §4/#3 | On each ARM64 and x86-64 host, record before/during/after noVNC the display framebuffer width/height, browser outer-window x/y/width/height, and page `screen.width`, `screen.height`, `screen.availWidth`, `screen.availHeight`, `innerWidth`, `innerHeight`, and `devicePixelRatio`. Open noVNC and return; verify the same run/tabs, full browser-window fit within the display, unchanged remote geometry and page values, successful manual input, and a successful ordinary click afterward. | ST-04, ST-06, ST-07, MB-02, MB-08, MB-10 | Per-host browser check | NOT RUN |

A passing diagnostic does not establish universal undetectability or CAPTCHA immunity. Configuration review also confirms virtual-display startup, `humanize=True`, new-Camoufox-profile Linux defaults, preservation of existing fingerprints, enabled WebGL, and no added Canvas/UA/font/`navigator.webdriver` overrides or invented GPU identity.

## 6. Focused supplementary boundaries

These checks cover consequential requirements outside the source acceptance tables; they do not require a broad rewrite of the existing test suite.

| Case | Executable check and required assertion | Requirements | Result |
| --- | --- | --- | --- |
| X01 — Helper launches and duplicate profiles | Race the stopped-profile cookie helper against normal launch for the same persistent directory; only one browser can own it. At full capacity the helper cannot spawn another browser. Existing supported live-run operations reuse that run without another place; do not add live cookie import/export or remove native stopped-profile preconditions. | GC-02, GC-03, GC-04, VR-06 | NOT RUN |
| X02 — Timeout and spawn-registration crash | Delay startup beyond the API wait while the process lives; occupancy stays reserved. Inject true failure/cancellation after partial spawn: release only after verified exit, once. Crash the parent between runner spawn and PID registration: the child must not create a browser before durable binding/handshake. After recovery or reclaimed reservation, a delayed child must not create an unrecorded browser or free a replacement run. | GC-06, GC-08, GC-09 | NOT RUN |
| X03 — Admitted batch outcomes | With sufficient places, admit three profiles and make one runner fail. Preserve the two successful runs, report per-profile outcomes, and free only the failed run after cleanup. Separately include duplicate and already-running targets and verify no double reservation. | GC-05, GC-07 | NOT RUN |
| X04 — Adapter, gateway, and close races | Race two manual opens across server processes, inject VNC readiness failure, close during startup, restart the gateway, and replay stale close/access. At most one manual reservation survives; no late startup reopens a closed session and no browser/display is killed by manual cleanup. | MB-03, MB-04, MB-07, MB-10, MB-11 | NOT RUN |
| X05 — Gateway boundary | Attempt cross-origin cookie-authenticated mutations/WS, expired or revoked access, arbitrary display/port/host/Xauthority targeting, public direct VNC access, and direct connection from an untrusted local process to the helper's backend channel while a session is active. Verify denials, the gateway-only private connected channel, server-side backend-auth termination, no backend credential in the noVNC configuration/API DTO/URLs/WebSocket traffic/browser storage/logs, structured process arguments, and no global X11 access bypass. | MB-04, MB-05, MB-06, MB-07, VR-03 | NOT RUN |
| X06 — Existing engine behavior | Exercise an existing Chrome profile's authorized management and shared-capacity accounting. Request noVNC: receive explicit unsupported-runtime behavior without a replacement browser or changed engine. | VR-07, MB-01, GC-03 | NOT RUN |
| X07 — No duplicate panel input | Instrument the controlled screen's REST requests and screenshot loop; during VNC, gestures go only through VNC and hidden screenshots stop. Back restores the existing handlers once; no new global API/MCP pause gate appears. | MB-09, MB-12 | NOT RUN |
| X08 — Native browser filesystem/process isolation | On staging under the actual service UID/namespace arrangement, use noVNC Ctrl+L/native open-save dialogs to attempt reading PB's canaries, service `.env`/config, extension state, and another process's `/proc/.../environ`; attempt a disposable write outside PA. All must fail without exposing protected contents. Inspect the browser's own `/proc/self/environ` path: service/session-secret canaries must not be inherited. Repeat equivalent supported navigation/evaluation paths. PA's permitted behavior still works; unavailable confinement fails closed instead of enabling an unrestricted browser. API field filtering alone is insufficient. | PA-09, PA-10, PA-14; MB-07, VR-05; Access §7/noVNC §9 | NOT RUN |
| X09 — Auth and file edge boundaries | Verify robust password hashing, logout/reset/session revocation, cookie flags/CSRF, and non-enumerating login errors. After first-admin initialization, reject unauthenticated bootstrap reuse. Import archives with traversal/symlink targets, privileged fields, and cross-profile IDs; fail without arbitrary writes or transient public profiles. An allowed export contains only its permitted data. | UA-02, UA-03, UA-04, UA-08, PA-02, PA-09, PA-12 | NOT RUN |
| X10 — Stable identity and residual directories | Rename an assigned profile and restart: its grants/data follow the immutable ID. Inject a catalog-save or deletion-cleanup failure; an incomplete result is reported and the affected data is quarantined or barred from name/path reuse. A different user's new profile cannot inherit the residual cookies, fingerprint, or grants by reusing the name. | PA-01, PA-12, UC-06 | NOT RUN |

## 7. Upstream-update and architecture acceptance

The goal is a small, reviewable integration surface with tested upgrade candidates. It is not a promise that arbitrary future upstream revisions will work without adaptation. The implementation agent executes mandatory baseline and synthetic-drift gates offline/CI and hands over evidence. U04 applies only when a concrete additional upstream candidate exists; the absence of one is not an initial-release blocker. The separate deployment agent verifies that the chosen live release matches that evidence; operational backup/restore in U05 is repeated on isolated staging in slice 09. A local/CI pass never substitutes for the two-host live matrix.

| Case | Executable check and required assertion | Requirements | Result |
| --- | --- | --- | --- |
| U01 — Fork diff and responsibility review | Compare the fork with its pinned upstream commit. Account for every core hook; new features live in cohesive extension modules/assets. Reject copied replacement panels/modules, runtime monkeypatches, vendor-engine patches, and unrelated refactors. Review the exact order `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY`: domain rules are independent of persistence/HTTP/process adapters; DAO/DTO boundaries serve real responsibilities without a universal framework. | UC-01, UC-02, UC-07 | NOT RUN |
| U02 — Finite adapter contracts | Record tested versions and run focused checks for the API/service call surface, profile identity/serialization, launch admission, runner readiness/display metadata, and existing panel integration. A deliberately changed adapter signature/descriptor in a test fixture produces a specific contract failure rather than silently losing a check. | UC-03, UC-07, VR-03 | NOT RUN |
| U03 — Newly added bypass surfaces | On a disposable candidate, introduce test-only unclassified profile-data and browser-creating entry points. The route/service/launch inventory gate must fail; calls missing principal/admission context must fail closed. Add proper classification/adaptation and verify only the focused regression then succeeds. | UC-04, GC-03 | NOT RUN |
| U04 — Conditional real update candidate | If a concrete additional upstream commit is selected, merge it into an isolated candidate worktree and run the required upstream suite once plus focused domain/adapter, access/capacity, noVNC, and copied-data migration checks. Without a selected candidate, initial release uses mandatory baseline/synthetic checks U01–U03 and this row is N/A, not a blocker. Never invent a successful hypothetical merge. | UC-05 | N/A — initial release without a selected candidate |
| U05 — Migration interruption and matched restore | Offline: exercise migration and matched restore on synthetic/test-copy fixtures. Final slice: the deployment agent repeats on an operational backup restored into isolated staging. Interrupt/retry/rerun migration; preserve profiles, identities, assignments, valid capacity, and service access. Pending imports remain inaccessible; restoration does not expose old unprotected mode. | UC-06 | NOT RUN |
| U06 — Fail-closed activation | Enable the extension with a missing adapter/security hook and separately an invalid schema/migration state. Protected startup fails with a redacted operator error; no partially protected panel/API or single-token fallback becomes available. | UC-08 | NOT RUN |

## 8. Resource measurement sheet

Use the same recorded page/tab workload and stable remote geometry within each comparison. For each row measure **before noVNC / during one connected noVNC / after Back and cleanup**. Record actual total memory, peak browser/display/adapter memory where separable, CPU, input responsiveness, and process counts; do not infer memory use from profile counts. The expected temporary VNC-process sequence is `0 → 1 → 0`, not a measurement already made.

| Host | Active browsers | RAM before/during/after | CPU before/during/after | Manual input observations | Browser/display/VNC process counts | Result |
| --- | --- | --- | --- | --- | --- | --- |
| ARM64, no GPU | 1 | Not measured | Not measured | Not measured | Not measured | NOT RUN |
| ARM64, no GPU | 5 | Not measured | Not measured | Not measured | Not measured | NOT RUN |
| ARM64, no GPU | 10 | Not measured | Not measured | Not measured | Not measured | NOT RUN |
| x86-64, no GPU | 1 | Not measured | Not measured | Not measured | Not measured | NOT RUN |
| x86-64, no GPU | 5 | Not measured | Not measured | Not measured | Not measured | NOT RUN |
| x86-64, no GPU | 10 | Not measured | Not measured | Not measured | Not measured | NOT RUN |

Record behavior after repeated open/Back cycles under a representative load to detect accumulating temporary resources. A host unable to sustain a load level receives a precise limit/failure record; default capacity 10, a 1366×768 baseline, and the absence of a GPU are not evidence of a particular RAM saving or throughput.

## 9. Completion gate

The implementation agent first completes O01–O07 and hands a locked release to the separate deployment agent. Only in final slice 09 does that agent complete the 30 Access, 13 noVNC, and 3 Stealth source cases, the relevant focused boundaries, and the two-host resource matrix. Capture all supported launch and data-access surfaces from the actual fork, not just panel buttons. Check copied-data migration and restore before production rollout. A completed implementation without live evidence is **ready for deployment**, not **live acceptance passed**. Final acceptance requires recorded mandatory evidence and resolved failures. Report unavailable required environments as blockers; a real update candidate not selected for initial release is conditional U04 N/A. Document validation alone satisfies none of those runtime checks.
