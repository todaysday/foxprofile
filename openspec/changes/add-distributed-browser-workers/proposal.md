# Change: distributed browser workers

## Why

FoxProfile currently runs its browser processes on the same installation that stores the profile catalog. With limited RAM on the primary VPS, users cannot use otherwise idle memory on other VPSs. The owner wants a single permanent Master copy of every profile, with its existing fingerprint and cookies, and remote browser execution on any available Ubuntu 24.04 Worker without ever running the same profile twice.

This change builds on the implementation branch codex/openspec-multi-user-manual-browser-workflow at e8a9c204fce7e8fb50b5827977e0567f196cdfaf, **not** main. That branch already adds durable SQLite capacity and run generations, profile IDs, authorization, process confinement, and noVNC in foxprofile_ext. The new capability must extend these mechanisms, not create a parallel profile manager or bypass access policy.

## What changes

- One authoritative Master stores catalog, profile data, fingerprints, cookies, users, grants, SQLite admission, web UI, API and MCP. Master can still execute local browser sessions.
- Lightweight Ubuntu 24.04 Worker services connect outbound to Master, report identity and available resources, and accept individually leased browser runs.
- When a new run does not fit Master's safe free-RAM budget, the scheduler assigns it to a healthy ready Worker. Assignment is immutable for that run; there is no live migration.
- Master sends an integrity-checked copy of a stopped profile and launch settings. Worker runs the existing Camoufox/Chrome engine, exposes control through Master, and returns changed persistent browser data after a clean stop.
- Central per-profile exclusivity and installation-wide capacity include every local/remote run and supported temporary helper. Failures and lost connectivity never permit concurrent copies.
- Current REST/MCP/web profile operations remain Master endpoints. Remote Playwright actions, screenshots and supported manual noVNC access route to the owning Worker.
- Deployment, not the scheduler, ensures consistent Ubuntu 24.04 builds, browsers, fonts, display geometry, renderer and sandbox configuration.

## Non-goals

No second Master, shared network profile filesystem, Kubernetes, Redis, new queue/broker, user database on Workers, public worker API/VNC ports, live browser migration, periodic incremental checkpointing, automatic process eviction, or fingerprint regeneration. Existing noVNC limitations for unsupported engines remain.

## Success criteria

1. Under memory pressure, a permitted launch is admitted exactly once and becomes controllable on a ready Worker through the existing Master API and MCP.
2. Successful stop persists browser state (including cookies, storage and tabs) on Master, checks fingerprint integrity and increments a canonical revision once.
3. Concurrent requests for the same profile, stale worker commands, network partitions, partial transfers, unknown process liveness and Master restarts never create a second live copy or overwrite the last committed revision.
4. A Worker independently stops its browsers when it loses Master connectivity. Master retains the last known-good profile, even if the latest session's unsaved changes are lost.
5. Every admitted Worker has a verified identical deployment contract for its engine/environment, regardless of which VPS it runs on.

## Impact and acceptance boundary

Affected code is primarily foxprofile_ext capacity/runtime/manual/storage plus narrow upstream launcher/transfer composition hooks, Worker systemd deployment artifacts, and minor web panel status additions. Prior authorization/confinement guarantees remain in force.

The existing multi-user/manual-browser change has unfinished deployment acceptance (slice 09). This is a spec-only change; no code implementation, live deployment or test execution is claimed here. See design.md, reuse.md, tasks.md and the capability specs for acceptance criteria.
