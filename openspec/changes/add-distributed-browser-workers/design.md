# Design: one Master, an Ubuntu 24.04 Worker pool

## 1. Selected topology

The only authoritative writer of profile data and capacity is Master. It retains the upstream profile JSON catalog and directory layout, foxprofile_ext SQLite database, authorization, REST, MCP and UI. Master may also launch a local browser via its existing RuntimeSupervisor. A remote Worker only executes browser processes against a temporary copy; it must not expose an independent profile catalog, personal login/API, MCP or public VNC service.

Each Worker initiates an authenticated outbound WSS control channel to Master. Profile archive download/upload uses separately authorized, worker-initiated HTTPS streams because large binary archives should not compete with control heartbeats. Temporary noVNC traffic uses an outbound, exact-run-bound binary channel via Master, not a public worker listener. All browser-facing client routes stay on Master.

The chosen baseline is commit e8a9c204fce7e8fb50b5827977e0567f196cdfaf of codex/openspec-multi-user-manual-browser-workflow. This builds on the existing foxprofile_ext extension seams and SQLite ledger; its previous protected installation rule of one lifecycle manager remains valid **per Master installation**, but remote Workers are a distinct, deliberately added execution role and do not share a copied SQLite database.

## 2. Role boundaries

| Owner | Responsibility |
| --- | --- |
| Master ProtectedApplication | Authenticate callers, recheck profile grants and resolve immutable profile_id; preserve UI/REST/MCP behavior |
| Master PlacementScheduler | Estimate safe capacity and choose local or remote host before atomic admission; never migrate live runs |
| Master CapacityLedger and SQLite DAOs | Single global max_concurrent_profiles limit; unique active profile_id; immutable run_id/owner; per-host reserved RAM; recoverable handoff and revision records |
| Master ProfileStore | Canonical fingerprint/browser data, versioned immutable snapshots, validated incoming commits, recovery journal |
| Master RemoteRuntimeAdapter | Commands and results keyed by exact run generation; no remote browser secrets exposed |
| Master ManualSessionService | Existing personal ACL and one installation-wide manual lease, now resolving a remote run owner if needed |
| Worker Agent | Mutually authenticated registration, telemetry and transport; gated start/stop; local crash cleanup; per-run isolated staging |
| Worker RuntimeSupervisor | Reuse proven local process identity checks, bubblewrap confinement, Xvfb and PageController; never become central admission authority |
| External deployment system | Install, version-pin, validate and update Ubuntu 24.04 workers and service units; FoxProfile does not manage SSH inventory |

The worker sees only data for its currently assigned run. Its credential identifies a machine/service, not a personal user. Master sends only the launch secrets required for the assigned profile, scoped to that run.

## 3. Standardized runtime, no per-profile OS scheduling

All servers are Ubuntu 24.04. All other relevant runtime components are installed under the same versioned deployment manifest: Python/dependencies, Camoufox and fingerprint-chromium binaries and hashes, fingerprint generator assets, fonts/fontconfig, tzdata/locale, sandbox, Xvfb, window/screen dimensions, renderer policy and browser options. Worker reports a manifest digest and preflight results; an incompatible/unprepared worker is never selected. Scheduling does not implement a platform matrix or pin profiles to named machines.

This uniform manifest is a necessary but not sufficient identity guarantee: deployment preflight compares real browser-visible geometry, font and WebGL output and actual rendering backend on each CPU architecture; if outputs cannot meet the agreed contract, mark the Worker incompatible. Fingerprint files remain byte-stable. Do not promise identical egress IP for profiles with no proxy; preserve the profile's configured proxy and exit-IP policy when moving machines.

For never-run profiles, Master must initialize the engine identity once in its own canonical profile before a remote snapshot. A Worker must reject an absent, invalid or inconsistent identity rather than silently allowing the baseline load_or_create path to regenerate it. Current intentional geo-locale changes following proxy country are separate from worker placement and may continue under baseline rules.

## 4. Worker enrollment and scheduling

Stable worker_id identifies enrollment; boot_id and connection generation identify a particular boot and transport. Credentials are unique, revocable and distinct from both personal sessions and Master API token. New connections fence older connections for the same Worker. Workers report version, manifest digest, engine readiness, usable memory, MemAvailable, effective cgroup headroom when constrained, free staging disk, reserved resources, live exact run generations and drain status. Units are bytes. Master timestamps reception in UTC and does not trust client timestamps for freshness.

Heartbeat interval is 10 seconds; after 30 seconds without authenticated fresh telemetry, Worker is stale/unselectable. Health and liveness are distinct: stale telemetry never proves a browser stopped. Worker states: registering, ready, draining, incompatible, offline. Admission requires ready, fresh, not draining, sufficient disk and a verified manifest.

Define memory headroom for placement as the minimum of estimated host MemAvailable and cgroup headroom (when present), minus system reserve and centrally reserved-but-not-yet-reflected run budgets. Estimate per-browser memory from measured workloads, with a conservative configuration value until enough evidence exists; no universal fixed browser size is asserted. Prefer Master while it has safe headroom, otherwise select the ready Worker with greatest **post-reservation** headroom, stable tie-break by worker_id. When no host fits, fail admission without evicting existing browsers or queuing work. Admission is a best-effort memory forecast, not an OOM-proof guarantee; Worker performs final pre-start guard.

## 5. Single authoritative run admission

For each launch:
1. Master authenticates principal and checks access to all requested profiles.
2. Resolve stable profile IDs and current canonical revisions; do not let user-supplied host IDs choose an authority.
3. Plan whole batch placements, including temporary helper browsers, using fresh node metrics and reservations.
4. In **one** short Master SQLite transaction, enforce global max_concurrent_profiles, existing unique-active-profile constraint and host RAM reservations; reserve new exact run_id and immutable assigned worker_id/boot generation (or local).
5. Outside the transaction, stage and transmit a snapshot; Worker verifies the dispatch grant/run identity and local confinement before spawn.
6. Readiness follows existing CONTROL-before-BROWSER_STARTED contract; caller timeout may return accepted/starting without freeing the run.

Concurrent schedulers must not oversubscribe: revalidate telemetry generations and reserved headroom inside the admission transaction, retry planning once on contention, then reject safely if no placement fits. A duplicate request for an already-owned profile returns the existing run as existing APIs permit, never a second browser. Worker may not self-issue a new run or bypass its gated start through the regular launcher.

After a confirmed stop, keep central profile ownership until data sync is committed or conclusively failed and isolated; merely removing a process from a local dictionary, receiving a timeout or seeing a stale heartbeat never frees the slot. Global count is across local and remote runs; adding noVNC or Viewer does not consume an extra browser slot. Global ledger migration must widen the existing active-run partial unique index and every occupancy/recovery query to include transferring, syncing and uncertain: the baseline index covers only reserved, starting, running and stopping. No transition may temporarily remove exclusivity.

## 6. Snapshot and canonical commit

Master stores a monotonic canonical revision and content digest for each profile_id. Runtime metadata/ACLs and personal sessions stay in Master SQLite; the worker receives only allowlisted browser profile data and launch configuration, never a portable administrative import with imported profile IDs.

Startup snapshot requires exclusive access to stopped data, and a recorded base_revision. Archive staging is private, bounded by byte count, member count and expanded size, with path/symlink, hash and free-disk checks. Worker downloads a short-lived run-bound archive over HTTPS into private staging, validates before atomic activation, then launches only that copy. A partial transfer cannot touch Master canonical data.

On normal stop:
- Stop accepting page/input/manual actions for that run, close the browser, and confirm its exact process tree ended before packing browser storage.
- Worker uploads the complete updated data with (profile_id, run_id, worker_id, worker_boot_id, base_revision, archive_digest), using a one-use scoped upload capability. Master validates identity files and canonical revision.
- Master records a durable commit intent, verifies the new snapshot and atomically promotes its staged directory using a crash-recoverable filesystem/SQLite journal, preserving the old known-good version until promotion is verified.
- Revision advances exactly once, and a stable commit receipt is returned. A retried upload after lost acknowledgment returns that receipt; it must not produce revision+2.
- Only after verified commit or explicit quarantine of an unsaved terminated session may central profile ownership be released.

A Worker must never send its old profile.json to overwrite later authorized Master metadata edits; only browser data changes are committed. Master honors baseline per-profile catalog editing semantics, but serialized edits to browser-data/identity while a remote copy is live are refused. Existing public export/import and stopped-only cookie helper operations must respect the global remote active lock.

If upload fails with a reachable Worker, retry the same upload with the same run/receipt; it cannot restart the browser to make the transfer succeed. If worker custody or final state becomes uncertain, retain the canonical revision, mark unsaved/blocked and never accept an unsolicited late upload. For local runs, retain existing direct on-disk operation while updating revision bookkeeping only after a safe confirmed stop.

No merge of individual cookies, continuous checkpoint stream or automatic reconstruction of lost intermediate writes is required.

## 7. Remote control and noVNC

Master preserves current personal ACL checks, REST routes, MCP tools, screenshots and panel Viewer. RemoteRuntimeAdapter routes a correlated action by (profile_id, run_id, owner_connection_generation) over WSS; Worker forwards to existing loopback-token PageController and returns bounded result/errors. Reject commands after stop, an outdated run, permission revocation or invalid worker identity. Neither control port nor tokens, Xauthority, PID or VNC backend secrets reach external clients.

The existing Master ManualSessionService remains responsible for one active/provisioning/closing manual lease across the installation. For a supported running Camoufox profile, an exact-run-bound remote backend on the owning Worker attaches to its actual Xvfb/Xauthority and streams RFB through the Master gateway; private x11vnc adapter remains invisible to untrusted host peers. noVNC Back releases input, helper and stream, not browser or global slot. Preserve the prior 60-second end-to-end manual cleanup maximum; if remote helper exit is unconfirmed, lease stays occupied. Do not silently enable unsupported Chrome noVNC modes or alter browser geometry on client viewport resize.

## 8. Loss, fencing and restart

A Worker must enforce connectivity loss independently of its WebSocket event loop: systemd-owned supervised browser cgroups and a watchdog armed **before** browser launch. Missing verified Master control for 30 seconds triggers stop of all browser process trees, including stuck child cases; the worker uses bounded TERM then KILL and records exact run termination evidence. If agent itself fails, service supervision must still stop owned browser processes. A short-lived reconnect before the loss deadline may preserve a verified still-current run; once the watchdog enters lost-contact shutdown it cannot be revived by reconnection.

Master marks an unreachable run **uncertain** and stops forwarding actions but does not assume it died. The unique active-profile reservation and memory claim remain. It may unlock/reassign only after Worker returns authenticated matching run/boot/process stop proof, or after a trusted external operator fences the whole old host and verifies shutdown. An unresponsive worker cannot be automatically presumed safe after an arbitrary TTL. Late old events cannot free a newer run. A disconnected Worker may lose last-session updates: canonical Master data remains intact, not rolled back to a corrupt partially imported snapshot.

On Master restart, persisted ledger, revisions and leases are recovered before new admission. Agents detect lost Master link and terminate if they cannot reestablish a valid current lease before the watchdog deadline. If cleanup evidence is unavailable, fail closed until resolved. Worker reboot yields a new boot_id and previous boot's process trees are dead only after verified reboot evidence; a bare reconnect claim is not proof.

## 9. Deployment, presentation and tests

Use the existing deploy/extension baseline as source for a worker-only Ubuntu 24.04 systemd deployment. Deployable artifacts are version- and hash-pinned, private service credentials, no exposed worker control/VNC listener, isolated run directories, configurable system reserve and bounded staging-disk budget. Master shows admin-only worker health/memory/active assignments; profile users see only their permitted profiles and an assigned-location/status label. No user can infer another user's profile names via capacity errors or telemetry.

Create a separate acceptance matrix for mock/isolated-process concurrency, local two-host WSS/HTTPS, native browser identities, manual input, restart and intentional link failure. Do not claim real deployment validation on the strength of source inspection. The previous change's unfinished live slice 09 must remain open.

## Traceability

Source and tests: references/evidence.md and reuse.md; bounded API/DTO contracts: references/contracts.md; test matrix: references/acceptance.md. Capability SHALL statements: specs/worker-lifecycle, specs/placement-capacity, specs/profile-synchronization, specs/remote-control, specs/failure-recovery.
