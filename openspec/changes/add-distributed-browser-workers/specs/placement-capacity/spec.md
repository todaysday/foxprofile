## Purpose

Distribute new browser starts by usable memory while retaining the existing durable installation-wide capacity ledger and guaranteeing at most one active run per profile across all machines.

## ADDED Requirements

### Requirement: DW-P01 Local-first memory placement
For a new authorized profile, Master SHALL select local execution when it has safe headroom; otherwise select a fresh, ready Ubuntu 24.04 Worker with enough projected headroom and staging disk. Worker selection SHALL consider MemAvailable, cgroup headroom when constrained, configured service reserve, and already reserved-but-not-yet-reflected runs. It SHALL use a deterministic tie-break and SHALL NOT live-migrate existing browsers.

#### Scenario: Master lacks safe memory
- **GIVEN** Master free safe headroom is below the new-run estimate and Worker B has sufficient fresh headroom
- **WHEN** the authorized launch is requested
- **THEN** Worker B is reserved without changing the profile identity or moving another running browser.

#### Scenario: No host fits
- **WHEN** Master and Workers have insufficient safe memory or stale telemetry
- **THEN** no new browser starts, no partial profile data transfer begins, and the caller receives a capacity or no-worker-memory error.

### Requirement: DW-P02 One transaction for global ownership and placement
Master SHALL extend the existing SQLite admission transaction to bind immutable profile_id, run_id, assigned worker/boot generation and memory reservation. The existing partial unique active-profile constraint SHALL remain authoritative for local and remote runs. A Worker SHALL NOT self-assign or start a profile without its exact valid Master reservation.

#### Scenario: Two concurrent starts of the same profile
- **WHEN** two authorized callers launch the same profile concurrently and different workers have space
- **THEN** no more than one run_id becomes active and no more than one browser starts globally.

#### Scenario: Stale dispatch is replayed
- **GIVEN** an older run has already been closed or invalidated
- **WHEN** its old start instruction arrives at a Worker
- **THEN** the Worker refuses to spawn and cannot alter the current run.

### Requirement: DW-P03 Global limit across all runtimes
The existing persisted max_concurrent_profiles SHALL count all reserved, transferring, starting, running, stopping, syncing and uncertain sessions on Master and remote Workers once per active profile. Browser-backed helper tools SHALL share capacity and exclusivity; Viewer/noVNC SHALL NOT consume a second browser place.

#### Scenario: Ten-slot installation-wide limit
- **GIVEN** eight remote and two local active/reserved profiles with limit ten
- **WHEN** another profile launch or temporary browser helper is requested
- **THEN** it is refused despite memory being free on a Worker.

#### Scenario: Existing remote run gains a control client
- **GIVEN** global capacity is full
- **WHEN** an authorized user opens Viewer or supported noVNC for an existing remote run
- **THEN** the browser count does not change.

### Requirement: DW-P04 Whole-batch atomic preflight
Authorization and capacity SHALL be evaluated across the whole batch before any new profile launches. The placement algorithm SHALL reserve enough host memory and global places for all new distinct profiles or none; browser startup failure after successful admission SHALL report per-profile outcomes without killing successful siblings.

#### Scenario: Two profiles compete for one remaining place
- **GIVEN** one global run slot remains
- **WHEN** a single batch requests two new authorized profiles
- **THEN** neither begins transfer or spawn.

#### Scenario: One target has no permission
- **WHEN** a batch includes an unauthorized profile
- **THEN** no placement or occupancy information about that profile is exposed and the entire new batch is denied.

### Requirement: DW-P05 Release requires verified stop and safe storage outcome
A remote run's exclusive profile lock SHALL remain until its exact browser process tree is confirmed dead and the data-sync outcome is committed or explicitly quarantined. Rejected/failed pre-spawn reservations may be canceled only after proving no child was created. Stale callbacks and timeouts SHALL NOT free ownership.

#### Scenario: Stop called but child still alive
- **WHEN** Worker acknowledges receipt of stop while browser shutdown is still pending
- **THEN** the run remains counted and cannot be reassigned.

#### Scenario: Old event arrives after another run
- **WHEN** a completion callback for an earlier run_id is replayed
- **THEN** it cannot free, stop or overwrite the newer generation.
