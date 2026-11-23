## Purpose

Make remote browser execution fail closed under disconnection, process crashes and restart, preserving last known-good Master data and the guarantee that a single profile never runs on multiple machines simultaneously.

## ADDED Requirements

### Requirement: DW-F01 Independently enforced loss watchdog
Before browser spawn, each Worker SHALL arm a liveness watchdog independent of the agent event loop. If there is no valid Master control lease for 30 seconds, Worker SHALL begin shutdown of all its owned browser process trees and orphanable Xvfb/manual children using bounded TERM then KILL under verifiable systemd/cgroup ownership. A dead Worker agent SHALL NOT leave an unmonitored browser usable indefinitely.

#### Scenario: Network cable is cut
- **GIVEN** Worker B is running profile P and Master becomes unreachable
- **WHEN** the configured loss deadline passes
- **THEN** an independent supervisor terminates the browser tree and records exact stop evidence when possible.

#### Scenario: Worker main process is killed
- **WHEN** Worker agent crashes with browser children alive
- **THEN** systemd/independent supervisor closes or kills the owned tree; Master still treats the run as uncertain until proof arrives.

### Requirement: DW-F02 Unknown is not dead
Master SHALL mark lost-contact runs uncertain, disable further control and preserve their unique active-profile and memory reservations. It SHALL NOT infer process death from heartbeat expiry, closed WebSocket, elapsed time or an untrusted Worker claim. No other machine can run that profile until exact authenticated stop proof or independently verified full-host fencing exists.

#### Scenario: Partition but old Worker may still be running
- **WHEN** a new launch request is made for the same profile
- **THEN** it is blocked, even if another VPS has ample RAM.

#### Scenario: Operator powers down unreachable host
- **WHEN** trusted independent host-level verification proves the old execution host is fenced
- **THEN** Master can retire only the matching old run and later permit a fresh reservation.

### Requirement: DW-F03 Reconnect cannot revive a fenced generation
Reconnect before the watchdog loss deadline MAY continue a verified same run if its Master reservation remains current and supervisor has not initiated shutdown. After watchdog-triggered shutdown starts, old runs SHALL not resume control or upload as if continuously connected. Older connection generations, callbacks and saved commands SHALL not change newer runs.

#### Scenario: Stale stop arrives late
- **GIVEN** Worker has rebooted and another run is active
- **WHEN** an old stop callback is replayed
- **THEN** it cannot alter that other run or free its reservation.

### Requirement: DW-F04 Master restart reconciliation before admission
A restarted Master SHALL reconcile its durable SQLite run ledger, worker connections, snapshot/commit intents and manual leases before accepting new launches. UNKNOWN liveness SHALL keep affected profile locked. A successful sync commit receipt or verified process shutdown SHALL be idempotent across restarts.

#### Scenario: Master crashes during active remote session
- **WHEN** Master comes back online
- **THEN** it cannot assign the profile to another worker until existing run status is verified, and the Worker watchdog stops if reconnection cannot be validated before deadline.

#### Scenario: Interrupted sync promotion
- **WHEN** Master restarts during canonical commit
- **THEN** journal recovery chooses one verified revision and cannot expose a partial or stale profile.

### Requirement: DW-F05 Explicit normal versus abnormal outcomes
Clean stop SHALL return current browser changes to Master before declaring fully saved; if that fails it SHALL expose syncing/failed-sync and retry or quarantine as appropriate. Abrupt disconnect MAY lose recent uncommitted changes but SHALL NOT corrupt the last Master copy. A worker must not automatically merge a late orphaned profile.

#### Scenario: Worker disappears before upload
- **GIVEN** a profile has a known canonical snapshot and no verified return archive
- **WHEN** its assigned Worker becomes unreachable
- **THEN** Master displays unsaved/uncertain, retains last committed data and refuses a second run until safe cleanup is proven.

### Requirement: DW-F06 No new infrastructure that weakens the baseline
The distributed capability SHALL retain existing user access checks, process confinement, global capacity and single-manual-session semantics. It SHALL NOT create a second writable Master, central broker, Redis, Kubernetes requirement or shared mounted live profile directory.

#### Scenario: Worker has no Master
- **WHEN** it has no valid authenticated Master assignment
- **THEN** it cannot accept a user-driven browser start or modify canonical data.
