# Tasks: distributed browser workers

**Change:** add-distributed-browser-workers.
**Selected branch:** codex/openspec-multi-user-manual-browser-workflow.
**Baseline:** e8a9c204fce7e8fb50b5827977e0567f196cdfaf.
**State:** specification complete; implementation and live acceptance NOT RUN.
**Priority:** preserve protected multi-user extension, catalog, browser profile identity and upstream compatibility; simple extension-owned components and narrow adapters.
**Dependency:** old add-multi-user-manual-browser-workflow slice 09 remains open and separate; do not report this package production-ready until both acceptances pass.

## Slice 1 — Worker enrollment and Ubuntu 24.04 preflight

- [ ] 1.1 Implement Worker-only service process and external deployment template. Worker holds no persistent profile catalog, users, grants, client API, MCP or public VNC; systemd owns its browser processes and isolated staging. Verify worker starts with no Master database on disk.
- [ ] 1.2 Implement unique authenticated worker enrollment, revocation and rotation, stable worker_id, boot identity and connection generation; connect outbound WSS. Tests deny counterfeit/stale worker connections and ensure credentials do not enter logs or browser children.
- [ ] 1.3 Report 10-second heartbeats and fresh memory/cgroup/disk/engine/active-run inventory; exclude 30-second-stale and draining workers from admission. Unit tests cover duplicate reconnect generations, invalid metrics and stale heartbeat.
- [ ] 1.4 Generate and verify pinned Ubuntu 24.04 runtime manifest: Camoufox/Chrome binaries, fonts, Xvfb, framebuffer geometry, actual renderer/WebGL, dependencies, sandbox, network/proxy and architecture-specific native checks. No per-profile OS matrix. Reject incompatible workers; record exact evidence.

## Slice 2 — Central admission and placement

- [ ] 2.1 Extend the existing SQLite capacity ledger/DAOs with worker identity, assignment generation, per-host reserved RAM and phase, retaining the current unique-active-profile index and global max_concurrent_profiles. Explicitly migrate the unique-active-profile index predicate and occupancy/recovery filters to include transferring, syncing and uncertain; test every state transition cannot open an exclusivity gap.
- [ ] 2.2 Implement local-first headroom selection using observed MemAvailable, effective cgroup budget, service reserve and projected outstanding allocations; otherwise pick highest safe Worker headroom with deterministic tie-break.
- [ ] 2.3 Keep principal authentication, profile authorization and batch preflight before transactionally reserving all new profile slots and placements. No network call under SQLite/catalog mutation locks. Whole batch fails before spawn if no capacity.
- [ ] 2.4 Include stopped-profile helper/cookie tools in shared exclusivity and admission; preserve existing full-limit and rename/delete/import rules. Tests cover local and remote race against same profile.
- [ ] 2.5 Race tests: independent Master API callers, simultaneous last Worker memory slot, duplicate same-profile requests, inactive/disconnected host, full global capacity and stale reservation callback.

## Slice 3 — Safe profile snapshot, transfer and commit

- [ ] 3.1 Add canonical revision and digest records per immutable profile_id; migrate existing stopped profiles without destroying data and initialize never-run fingerprint on Master exactly once.
- [ ] 3.2 Under common exclusive stopped-profile policy, build immutable allowlisted snapshot plus base_revision; authorize short-lived exact-run Worker HTTPS download. Validate archive size, member paths, symlinks, checksum and staging disk before local activation.
- [ ] 3.3 Bind Worker process to a writable private copy only after successful transfer; do not generate/reset fingerprint remotely. Ensure existing browser launch parameters match canonical identity and installed engine manifest.
- [ ] 3.4 On confirmed normal stop, stream complete updated browser data (not only cookies) back to Master; verify identity, owner/run generation and revision, journal filesystem promotion and SQLite revision atomically through recoverable intent.
- [ ] 3.5 Implement idempotent upload/receipt and crash recovery: repeated same upload commits once, corrupt/mismatched/late uploads cannot overwrite old revision, failed promotion leaves last good copy usable.
- [ ] 3.6 Tests: cookie/local-storage/tabs persistence, master metadata edits remain authoritative, transfer interruption, disk exhaustion, fingerprint mutation rejection, session cleanup and stopped-only helper exclusion.

## Slice 4 — Remote runtime control and UI

- [ ] 4.1 Add a bounded correlated control transport from Master RemoteRuntimeAdapter to Worker local PageController keyed by exact profile_id/run_id/connection_generation; preserve existing REST/MCP/UI contracts.
- [ ] 4.2 Route navigate, click, keyboard, text, tabs, screenshot and errors to exact remote run. Recheck personal/principal authority on every operation; reject stale generation and revoked access.
- [ ] 4.3 Extend minimal Worker inventory/active-host/sync state UI with admin-safe metadata; ordinary users see only authorized profile state. No separate remote panel or new user permissions.
- [ ] 4.4 Tests: Master + remote Viewer/MCP parity, authorization and privacy, large screenshot bounds, control timeouts and races with concurrent stop.

## Slice 5 — Existing noVNC across remote Workers

- [ ] 5.1 Extend the existing exact-run ManualSessionService to resolve remote runtime descriptors through Master while preserving a single installation-wide lease and current Camoufox support scope.
- [ ] 5.2 Establish outbound private binary RFB relay from Worker helper to Master WSS gateway; attach to the same worker Xvfb/Xauthority, not a new browser/display or public VNC listener.
- [ ] 5.3 Reauthorize stream, revoke on personal access loss, Back and stale run, release keys/buttons and terminate only manual helper within the prior 60-second cleanup window; keep lease blocked if cleanup unconfirmed.
- [ ] 5.4 Tests: same run before/during/after remote noVNC, one manual lease across hosts, foreign connection denial, forced disconnect and return to Playwright input; never increase global browser count.

## Slice 6 — Independent worker loss and Master recovery

- [ ] 6.1 Arm an independent Worker connectivity/lease watchdog before spawn; detect 30-second loss, stop owned process groups/cgroups, confirm exact termination and avoid orphaned Xvfb/helpers when Agent crashes.
- [ ] 6.2 Master marks disconnected runs uncertain; keeps global exclusivity and memory claim until authenticated exact stop receipt or trusted out-of-band host fence. No time-only unlock or automatic live migration.
- [ ] 6.3 On Master restart, reconcile durable runs/worker generations/revisions before admission; reject stale events, replayed uploads and reconnection after shutdown has begun.
- [ ] 6.4 Tests: network blackhole, agent SIGKILL, Master restart, host reboot, stale connection, upload interrupted by loss, proof unavailable, verified fence and recovery. Confirm at most one browser ever exists for any profile.

## Slice 7 — Acceptance and rollout

- [ ] 7.1 Run fork unit/integration suites and the focused matrix in references/acceptance.md; confirm baseline authentication, capacity, Xvfb confinement, transfer, page control, noVNC and helper regressions still pass.
- [ ] 7.2 Deploy to isolated Ubuntu 24.04 nodes only after the previous change's pending production-acceptance blockers are handled. Record actual dependency hashes/renderer, 1/5/10 browser RSS and safe headroom, proxy egress, profile persistence and forced loss behavior.
- [ ] 7.3 Verify staging/production backup and restore of Master SQLite + catalog + canonical profile directories, Worker secret rotation, drain, removal and restoration without accepting stray worker data.
- [ ] 7.4 Keep the change unarchived until separately measured two-host live checks and recovery evidence pass. Do not mistake unit tests for deployment acceptance.

## Evidence ownership

Specs and design define behavior, not executable code. Record actual implementation diff, test command/result, manifest digests, version and known blockers as implementation evidence. Preserve follow-up items in repository-level openspec/TODO.md rather than forgetting deferred scope.
