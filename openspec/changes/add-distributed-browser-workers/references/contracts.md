# Distributed internal contracts (design, not implemented API)

## Entities and authority

- **worker_id**: stable Master-registered machine identity, not trusted solely because caller supplies its string.
- **worker_boot_id**: distinct operating-system boot; **connection_generation**: new generation after authenticated reconnect. Control messages for old generations are stale.
- **profile_id**: immutable Master identity, never authoritative from portable archives.
- **run_id**: fresh Master-issued unique browser generation; assignment to one worker is immutable.
- **base_revision**: monotonic Master canonical browser-data revision from which the Worker started.
- **archive_digest**: strong full-content checksum of canonical or staged data; verifies full archive integrity, not authorization.
- **host_id / process identity**: exact local process boot/PID-start proof meaningful only to the owning host.
- **memory_*_bytes**: integers measured at the same heartbeat; timestamps for freshness are Master reception times.

The master ledger is authoritative for (profile_id, run_id, placement, state, revision). Worker claims cannot authorize themselves. Statuses are typed results and never carry proxy passwords/cookies or raw control secrets to a browser client.

## Connection handshake

Authenticated Worker enrollment returns accepted worker_id, boot/connection generation and approved runtime digest. Every command and reply must include a correlation identifier, profile_id, run_id and connection generation; duplicate command IDs get the previous result or an idempotent no-op. Worker telemetry contains MemAvailable, optional cgroup headroom, disk headroom, engine readiness, running exact generations, drain and preflight status. Missing, implausible or stale measurements make Worker unselectable; they do not terminate known runs.

Every valid memory sample includes the Worker's own nonnegative integer
`memory_reserve_bytes`. Master applies that reserve to that node and retains
charges for reservations absent from the sample's `memory_accounted_runs`.
`metrics: null` clears placement eligibility; `active_runs: null` means unknown
inventory and preserves known runs. Without a memory sample,
`memory_accounted_runs` is empty. Fresh authenticated heartbeats continue to
renew the current control lease when telemetry is unavailable. The loss deadline
must exceed the 10-second heartbeat interval and be no greater than 30 seconds.

## Run phases

**reserved → transferring → starting → running → stopping → syncing → closed**.

Terminal failure with verified absence of processes/clean staging can become closed; any uncertainty yields **uncertain/blocked**, and the unique per-profile reservation is preserved until independently verified stop/fence. Clean stop + save has a commit receipt. A cancellation before a verified spawn claims nothing; cancellation racing spawn must wait for exact process death.

## Transfer capabilities

A short-lived transfer grant binds worker_id, boot generation, profile_id, run_id, operation (download or upload), base_revision, expected digest and size limit. It cannot authorize a different profile, overwrite a catalog record or be reused for a different generation. Download only the snapshot after exclusive stopped-profile capture. Upload only after confirmed browser exit. Master stores durable commit intent and idempotent receipt keyed by exact run; a repeated identical upload returns the original receipt, while changed digest for the same receipt is rejected.

Archive allowlist includes engine-specific persistent data and identity files. Exclude secrets belonging to Master, ACL, logs, worker enrollment, OS sockets, temp locks, native PID/control descriptors and unrelated files. Validate expanded size and symlink/path traversal; worker staging cannot be promoted before verification.

## User/API behavior

Existing profile names and routes remain valid facade locators resolved at Master to profile_id. New admin worker metadata and run placement can be exposed through new protected API fields without requiring clients to call Workers directly. Existing MCP tools use Master only. A user cannot select a foreign host through untrusted launch parameters. Worker admin visibility never exposes another user's profile names in ordinary-user responses.

## Error families

AUTH_REQUIRED / PROFILE_FORBIDDEN (existing), PROFILE_BUSY, CAPACITY_FULL, NO_WORKER_MEMORY, WORKER_UNAVAILABLE, RUNTIME_INCOMPATIBLE, TRANSFER_INVALID, TRANSFER_CONFLICT, RUN_UNCERTAIN, SYNC_FAILED, STALE_RUN and MANUAL_UNSUPPORTED_RUNTIME. Public errors are non-secret and do not reveal unauthorized profile identity. Repeated idempotent commands return stable results rather than changing state twice.
