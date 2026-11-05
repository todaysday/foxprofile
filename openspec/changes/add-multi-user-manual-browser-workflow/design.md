# Design: maintainable access, capacity and manual control

## 1. Status, sources and priority

This is an implementation design, not an implementation report. Source-audited baseline: FoxProfile 2.4.0, commit `530b993ff3a7d211aaa0904fba0817b77c75d4c9`. The installed production revision is not known and must be recorded by the final deployment agent. Repository findings and pinned links are in [upstream-audit.md](references/upstream-audit.md).

The three original documents are included unchanged under `references/inputs/`. Normative capability specs define acceptance. This design resolves integration details and may be adapted only while preserving those requirements. Later user instructions are binding: maximize resilience to upstream updates; apply **Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY**; assign real deployment and live acceptance to a separate agent in final slice 9.

### Resolved boundaries

| Question | Decision and reason |
| --- | --- |
| User-access document excludes noVNC | That exclusion scopes the access document alone. The combined requested package includes noVNC from its separate source. |
| Who can see the profile occupying the manual slot? | Only callers already authorized for it. Others receive a generic busy message; access isolation takes precedence over the noVNC example that names the busy profile. |
| Ten active browsers | Installation default, not per-user quota or an unchangeable ceiling. Saved profiles are not counted. About 100 saved profiles is a workload, not a hard quota. |
| One manual session | At most one active or provisioning manual lease per installation, independent of browser capacity. No cross-host pool. |
| Automation while noVNC is open | No new arbiter or pause service. The agent is already waiting by the existing workflow. Suspend only the ordinary input handlers and hidden screenshot polling of the panel displaying noVNC. |
| `headless=False` in current runner | Correct inside the existing virtual branch: Xvfb is already created and passed to Camoufox. Do not blindly replace it with another `headless="virtual"`. |
| Chrome | Keep existing operations and common access/capacity policy; initial noVNC support is Camoufox-only. Do not restart or convert unsupported runs. |
| Profile changes versus stable identity | Explicit authorized edits still work. Login, assignment, launch/reconnect and noVNC do not regenerate fingerprint or change proxy/geometry. |
| Profile storage | Preserve upstream catalog and directories. Add one stable ID seam and an extension sidecar, with narrowly scoped cross-store operation recovery. |

## 2. Architecture and code ownership

Use a top-level `foxprofile_ext/` Python package for new code, with packaged browser assets and deployment documentation alongside it. This proposed name avoids spreading fork-only implementations across high-churn upstream files; it is not an existing upstream plugin API. Add it to packaging explicitly. Do not build a general plugin framework.

Responsibilities, not mandatory directories:

| Boundary | Responsibility |
| --- | --- |
| Authentication/application facade | Resolve a personal or trusted-service principal; invoke policies before protected operations; adapt existing UI/API/MCP calls. |
| Access policy | Two-role and full-profile authorization, data visibility and last-active-admin rule. No SQL, HTTP or VNC calls. |
| Capacity policy/application service | Atomic admission decisions, per-profile exclusivity, reservations, batch preflight and lifecycle transitions. |
| Manual-session service | One manual lease; exact run binding; authorize create/connect/close; manage the adapter's temporary resources. No agent concepts. |
| DAOs | Transactions and persistence for users, sessions, assignments, settings and leases. Small concrete repositories; no interface for every table merely for symmetry. |
| Upstream catalog/runtime adapters | Contain the few calls dependent on upstream signatures, serialization, process events and Viewer mounting. |
| DTOs | Validated requests/responses and internal run descriptors. Reuse existing Pydantic/dataclass patterns; never serialize database rows wholesale. |

Domain rules remain testable independently of FastAPI, Flet, SQLite and running browsers. Existing services are reused through explicit composition hooks. No service locator proliferation, universal manager, message bus, copied upstream classes, source regex patcher or runtime monkey-patching.

### Integration seams to keep small

| Existing location | Minimal integration responsibility | Compatibility contract |
| --- | --- | --- |
| `src/core/container.py` and existing interfaces | Compose protected facades/adapters and extension DAOs. | API and direct Flet paths resolve the same policy services. Missing caller authority does not become service access. |
| `src/api/auth.py`, `dependencies.py`, `app.py`, `mcp_http.py` | Personal principal, service-only machine context, mount new routes/assets, public-route allowlist. | No personal request can inherit the global MCP token; new protected routes default to authenticated handling. |
| `src/models/profile.py`, profile manager/transfer serialization | Preserve immutable local `profile_id`, allowlisted user settings, portable serialization, visible save errors. | Create/load/rename/overwrite round-trips keep identity correctly; portable archive cannot inject local authority. |
| Browser launcher/process/display/runner | Admission before spawn; runtime identity/display registration; verified cleanup notifications. | CONTROL-before-ready ordering, process start-time checks, count retained until real exit, existing Xvfb ownership. |
| Cookie service/helper | Shared admission/exclusivity and configured Camoufox virtual policy for real helper launches. | No capacity bypass; wait timeout does not leak an uncounted browser. Helper need not be exposed as an interactive noVNC run. |
| Existing bulk actions | Call one new batch service instead of fan-out singles. | All-target authorization and atomic capacity admission. |
| `src/web/index.html` and relevant Flet actions | Small login/current-principal, visibility, admin, bulk and Viewer mounting hooks; new UI modules/assets hold behavior. | Existing profile controls remain; no duplicate REST input while embedded noVNC is active. |
| Server/deployment composition | Register extensions, virtual mode before imports, same service namespace, WebSocket forwarding. | Protected mode cannot silently start without its migrations/hooks. |

The exact changed-file register is an implementation output, with one reason and one contract test per logical seam. This is a finite integration surface, not a promised fixed line budget: correctness at every protected route is more important than hiding unsafe changes behind an artificially small diff.

## 3. Identity, authentication and access

Personal users have exactly `admin` or `user`. A trusted service principal is a separate authentication kind, not a third UI role and not a user-controlled field. Keep current machine access explicitly configured and service-only for MCP. A personal session cannot be forwarded through MCP using the global token. Loopback origin is not authentication.

Use opaque revocable personal sessions with server-held token hashes, password hashes via a maintained library, HttpOnly/Secure/SameSite cookies under HTTPS, CSRF protection for state changes, and explicit Origin validation for WebSockets. Reuse an existing reliable auth library if already available; otherwise choose and pin one small maintained password-hashing dependency. No registration or email recovery. Administrator password reset revokes affected sessions. Bootstrap is a server-side command or existing secure setup mechanism, never a permanently open admin-creation route.

Authorization checks precede reads, exports, mutations and launches. Resolve tab/browser/viewer/file handles to a profile on the server; reject caller-supplied authority fields. The frontend receives only filtered data, not all profiles plus client-side hiding. Filter search, counts, running lists, groups, logs, proxy libraries and download resources too. Administrator-only routes remain server-checked even if UI buttons are hidden.

Revocation closes or gates existing screenshot/VNC streams and denies the next command. Access revocation does not stop the browser or reset its state. Administrative role changes use transactional last-active-admin protection. Every exposed application method receives an authenticated principal; direct-service callers do not get an implicit administrator default.

### Native-browser filesystem boundary

The audited service runs browsers under one service UID. noVNC exposes browser chrome and native dialogs, so a user can attempt local-file navigation without going through the REST navigation validator. API ACLs and one X display per profile therefore do not by themselves establish the required isolation of sibling cookies, application secrets or the extension database.

Before enabling personal noVNC, implement the smallest adequate per-profile process/filesystem confinement through the runtime spawn adapter. A Linux namespace sandbox or an already available equivalent may expose the required read-only runtime assets and the selected profile/staging directories while hiding sibling profiles, service configuration/database and other processes' environment. Select and document the concrete mechanism in the offline integration spike; keep it behind one narrow adapter rather than introduce a container platform. Sanitize inherited child environment so service credentials never reach an interactive browser process. Preserve verified proxy networking, profile persistence, X authorization and host-visible process tracking.

Do not claim that blocking `file://` in REST, hiding a file-picker button, or configuring a browser preference alone closes this boundary: test native URL entry, dialogs, downloads/uploads, filesystem links and `/proc` access in the isolated harness and again on each live host. If the host cannot enforce the required boundary, personal manual access fails closed and final acceptance is BLOCKED; it must not silently switch to a trusted-users-only interpretation of the supplied access requirements.

## 4. Persistence with minimal upstream churn

### Catalog identity

Add immutable local `profile_id` to the existing profile model and local catalog serializer/loader. Generate missing IDs once during an idempotent migration under an installation lock. Preserve the ID on rename and authorized overwrite; create a fresh ID on new profile, clone or new import. Imported IDs, grants, role fields, paths and runtime metadata are never authoritative. Existing name-based API inputs resolve to an ID within the protected adapter for compatibility; existing names remain labels and lookup keys, not authorization keys.

Keep browser directories and the upstream JSON catalog as the existing data sources. Split local serialization from portable export explicitly so exported ZIPs retain their existing portable metadata and omit server-local IDs, sessions, ACLs and tokens. The upstream loader currently selects known fields; a compatibility test must catch future code that drops the new ID. Do not regenerate IDs during every load.

### Additive extension database

Use one local SQLite file through DAOs, with schema versions, short transactions and suitable permissions. This needs no separate database process. At minimum it represents:

| Record | Key content |
| --- | --- |
| User | ID, unique username, password hash, role, active flag, revocation/version state. |
| Session | Hashed opaque session identity, user ID, expiry/revocation. |
| UserProfileAccess | Unique user/profile pairs; every pair grants the same full control. Administrator access is implicit. No sharing-permission editor. |
| GlobalSettings | Validated shared `max_concurrent_profiles`, default 10 only if absent. |
| BrowserRun/reservation | Installation/host ID, immutable profile ID, unique run ID, lifecycle, verified process identities and private runtime descriptor. |
| ManualSession | Opaque session ID, requesting personal session/user, profile/run/host binding, lease state and cleanup metadata. |
| CatalogOperation | Bounded pending operation intent needed to recover a catalog/ACL/filesystem change. No generic event-sourcing system or activity-audit product. |

The profile relation can naturally contain multiple assignments without creating a new UI permission hierarchy. Creating/importing a profile gives its creator an assignment; new users receive no automatic legacy profiles.

### Catalog mutation protocol

SQLite and a filesystem rename cannot be one transaction. Use a small explicit recovery protocol around existing catalog operations:

1. Acquire the common cross-process catalog mutation lock, reload the upstream catalog rather than trust a stale worker cache, resolve IDs and recheck actor authority.
2. Record a pending operation and its intended identity/assignment effects in SQLite. Hide an uncommitted new or changing object from ordinary users; pending state never means public/unowned access.
3. Perform the existing narrowly adapted catalog/filesystem operation, using a private staging directory for imports. Check persisted results and surface save errors instead of treating the upstream swallowed exception as success.
4. Commit the assignment/catalog-operation result only after the local catalog is durably consistent; release the lock. A failed operation rolls back or remains explicitly quarantined for deterministic recovery.
5. Before serving protected data after restart, recover pending operations using recorded IDs and paths; never infer ownership merely from a reused name.

Deletion first revokes grants and streams, respects browser shutdown rules, and tombstones the identity. A residual browser-data directory must not be accepted by a new profile with the same name. Finish verified cleanup or quarantine it; `rmtree(ignore_errors=True)` is not proof that cookies are gone. Rename tracks the filesystem move and catalog update as one recoverable operation. Overwrite checks target access before extraction and retains that target's identity/grants. Keep these finite operations small; no wholesale storage migration.

## 5. Shared capacity and runtime lifecycle

`running_count()` is a UI/runtime convenience in upstream, not capacity authority. Persist reservations before spawn and count reserved/starting/running/stopping profiles. Do not hold a database transaction while launching a process or making a network call.

Admission sequence: authenticate → resolve/authorize all targets → take deterministic profile locks/exclusivity → transactionally read current limit and occupancy → reserve all required new runs → commit → spawn through the existing launcher. Use unique active-profile identity and unique run IDs to reject duplicate starts. The batch service performs one preflight/reservation transaction; existing UI loops must call it rather than issue independent starts.

Close the spawn/registration crash window explicitly. Before it creates Xvfb or a browser, the child-side supervisor must atomically claim the matching reserved run and durably register its verified host process identity, or supply an equivalent OS-owned run marker observable after parent death. An already cancelled/reclaimed generation cannot pass that gate and spawn later. Recovery distinguishes an unclaimed reservation from a claimed live child; it must not infer that an absent parent-side in-memory handle means no process exists. Keep any database/privileged registration stage outside the interactive browser's confined environment, and do not expose its credentials to that browser.

For capacity rejection no new process starts. After successful admission, runtime startup can still fail for one target; report per-target results and clean that run without terminating successful siblings. There is no queue, priority, eviction or per-user quota.

The API's existing accepted/starting response on an HTTP wait timeout is not a failed launch. Keep its reservation. Cleanup, cancellation and process-exit callbacks release only the matching run after verified termination, idempotently. Stopping remains counted even when upstream removes a handle from its memory map. Cookie helper launches count too, and must share same-profile locks and configured launch policy.

Before new launches after manager restart, reconcile persisted descriptors with actual process start times and run identities. Preserve genuinely live runs; remove only proven stale reservations. If liveness is uncertain, disable new admission until reconciled instead of resetting the count. Existing display cleanup checks are retained. A lower limit preserves already admitted sessions; new ones wait for the user to retry after occupancy drops below the new limit.

Initial deployment retains the existing single lifecycle-manager topology. The ledger and exclusivity must still pass a two-process admission race test. Do not imply SQLite automatically makes process-local control handles work across arbitrary ASGI worker counts. If the fork enables multiple server workers, each must use the shared registry and verified runtime control adapter; otherwise reject that unsupported deployment configuration clearly. No additional network orchestrator is required.

## 6. Virtual runtime and stealth

The audited runner already owns `VirtualDisplay`, emits CONTROL before BROWSER_STARTED, uses saved fingerprints, and passes `humanize=True`. Extend its private metadata with profile/host/run IDs and actual display/Xauthority/namespace association. Reuse `display.py` PID/start-time cleanup and `process.py` lifecycle; never add a parallel display manager.

`FOXPROFILE_HEADLESS=virtual` must be set before configuration imports on both target servers. Its current implementation uses headed Camoufox inside the already allocated Xvfb. Browser-backed cookie tools also need the shared policy rather than their current hardcoded true-headless launch; extract only the small launch-option responsibility needed by both paths.

Default new Camoufox profiles to Linux and, where consistent and fully fitting, 1366×768. Do not mass-edit existing profiles. Keep native WebGL and Canvas behavior, saved fingerprint/cookies/proxy, stable locale/timezone and persistent directories. Add no JS UA/font/Canvas/webdriver spoofing and no mouse-trajectory generator. Humanize remains a Playwright/Camoufox behavior; do not claim it processes native VNC movements.

All engines obey access/capacity. Unsupported Chrome or legacy true-headless runs receive an explicit manual-access error without replacement, restart or engine change. Geometry is a relation among X screen, browser window and reported fingerprint; record and test them separately. noVNC uses local scaling, not remote resize.

## 7. Temporary noVNC sessions

One installation-level manual lease covers provisioning, active and closing states. Claim it atomically before starting a VNC adapter so simultaneous opens cannot create two adapters. Duplicate create from the same personal session/run may reuse the same session; conflicting requests return `MANUAL_SESSION_BUSY`. Do not return another user's connect credentials even when they also can access the profile.

Bind each session to authenticated user/session, installation host, profile ID and unique browser run ID. The browser supplies none of the display/process/port selection. Check profile authorization and run binding on create, status, connect, reconnect and close; active-stream revocation must work across the supported process model.

Prefer same-origin cookie-authenticated WSS with Origin validation, avoiding permanent credentials in URL parameters. A manual-session ID is a locator, not sufficient authorization. If deployment requires a ticket, make it short-lived, single-use, scoped to the requesting session and exact run, redact it, and revoke it on close. Do not add a ticket service merely to avoid same-origin cookies.

Initial adapter: a pinned distro-supported x11vnc build attached to the existing display through a gateway-owned private connected socket/subprocess channel, with no public listener. The [official x11vnc manual](https://github.com/LibVNC/x11vnc/blob/master/x11vnc.1) documents `-inetd` for a stdio-connected session; verify the selected build's connected-socket behavior and keep stderr separate from the binary stream in the offline spike. No inetd service or new desktop is required. Keep invocation behind a replaceable adapter because the upstream README reports maintenance concerns. Run helpers with access to the existing service's X namespace; retain `PrivateTmp=true`, Xauthority and filesystem restrictions. No public VNC port, global `xhost +`, unnecessary file transfer or shell-concatenated user arguments.

For local isolation, loopback binding alone is insufficient: the preferred private connected channel is available only to the authenticated gateway and helper, and needs no client-visible backend password. A fallback local TCP/RFB-password backend requires either an equally isolated transport or a gateway that actually terminates backend authentication. A plain byte proxy does not hide an RFB password challenge from noVNC; do not claim both that the client answers it and that it never receives the credential. Prefer the private channel rather than adding an RFB authentication subsystem unnecessarily. The manual lease is not released until its VNC child is confirmed gone; a replacement cannot inherit a still-live old bridge.

### UI and close lifecycle

Place **«Открыть в noVNC»** above the existing Viewer image. Mount noVNC in the same screen and preserve its surrounding navigation. Use `scaleViewport=true`, `resizeSession=false`, `viewOnly=false`. While it is mounted, suspend that panel's screenshot polling and REST mouse/keyboard handlers. This does not pause the agent or other profiles.

Back: disable manual input → authorize and close the matching manual session → revoke access/close WebSocket → release held keys/buttons and terminate only the VNC helper → restore a fresh ordinary Viewer frame and its normal handlers. Handle repeated closes safely. Disconnect/navigation/expiry perform equivalent temporary cleanup, with an explicit bounded grace interval if reconnect is supported. Reconnect reauthorizes the same still-current run; a new run requires a new manual session.

If the browser stops or its run identity changes, invalidate the session immediately; a stale display number cannot attach to its replacement. After normal closure and forced disconnect, verify the next ordinary Playwright click behaves correctly without a stuck drag/key. No path sends the agent a message or decides whether CAPTCHA succeeded.

## 8. API and error contracts

Use the existing `/api/v1` router style and existing response conventions. Proposed new operations and DTO boundaries are in [api-contract.md](references/api-contract.md); they are design contracts, not assertions about current upstream endpoints. Keep name-based legacy requests compatible by resolving server-side IDs; use stable IDs in new routes.

Errors have stable machine codes and redacted user messages. At minimum: `AUTH_REQUIRED`, `FORBIDDEN_OR_NOT_FOUND`, `PROFILE_CAPACITY_EXCEEDED`, `PROFILE_BUSY`, `MANUAL_SESSION_BUSY`, `MANUAL_UNSUPPORTED_RUNTIME`, `STALE_BROWSER_RUN`, `MANUAL_SESSION_CLOSED`, `RUNTIME_RECOVERING`, and validation errors. Reuse an existing structured envelope where possible; MCP translates the same domain outcomes rather than wrapping everything as generic launch failure. Document deliberate status-code changes for machine clients.

## 9. Upstream updates and verification

New code remains extension-owned; upstream hooks are explicit, documented and separately reviewable. Preserve upstream commit ancestry and avoid history-wide reformatting. Pin the tested revision in a compatibility record, not a permanent ban on updating dependencies. Keep extension dependency declarations separate where the upstream packaging allows it; no unsolicited major upgrades or modified vendored Camoufox/Flet/noVNC sources.

Tests target concrete risks: route/service coverage and missing-principal denial; profile identity/serializer round-trips; IPC readiness and process-event contracts; batch atomicity and two-process capacity races; parent death between runner spawn and registration; helper launch cleanup; same-run manual access; migration interruption and residual-directory safety. Existing upstream tests are run on update candidates to reveal accidental breakage; extension tests are additive, not modifications that make old tests accept weaker behavior. UI snapshots alone cannot establish server authorization.

Prepare an update candidate in a disposable branch/worktree: merge the desired upstream revision, inspect new routes/launch sites, adapt the finite hooks, run upstream and extension checks on copied data, and record a compatibility result. Route/launch inventory differences must require classification; runtime protected boundaries reject unknown authority so an inventory check is not the sole security defense. Passing offline checks creates a deployable candidate; final live testing/promotion belongs to the deployment agent. See [upstream-update.md](references/upstream-update.md).

## 10. Migration, rollout and responsibility split

Implementation agent writes and tests migrations, bootstrap, deployment assets, health checks, reports and rollback instructions on fixtures/copied disposable data only. It does not contact, deploy to, migrate or accept the real servers.

Final-slice deployment agent receives the exact tested fork commit and artifact hashes, schema/dependency versions, offline evidence, hook register, config example, migration/backup commands, credentials delivery method, and expected rollback compatibility. It records actual host/installed revisions, takes consistent backups of application state and closed browser profiles, rehearses on an isolated copy, deploys incrementally, and performs the source acceptance matrix on both architectures. Existing browser cookies and identities must survive. Increasing or lowering the capacity setting never forces the existing browsers to stop merely to meet a new numeric limit.

If old live sessions cannot be adopted safely during initial schema conversion, the runbook must use an explicit maintenance/drain procedure with preserved data, not pretend attachment or count recovery succeeded. Ordinary later manager crashes must satisfy live-run recovery requirements. A rollback uses a matched code/config/data snapshot; starting an unmodified shared-token upstream panel over multi-user protected data is not a safe downgrade.

Slice 9 and its report stay separate from offline completion. Required live tests that cannot run are recorded as BLOCKED/NOT RUN with the precise cause, never as passed. No claim of ten-browser capacity or stealth success comes from a document-only check.

