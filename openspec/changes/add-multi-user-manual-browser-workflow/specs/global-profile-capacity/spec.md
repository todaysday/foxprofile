## Purpose

Enforce one durable, race-safe limit on concurrently active profiles across every entry point of a FoxProfile installation, while preserving access to already running browsers.

## ADDED Requirements

### Requirement: GC-01 Persistent installation setting
The system SHALL persist `max_concurrent_profiles` as an installation-wide integer of at least 1, defaulting to 10 only when absent. Only an administrator SHALL change it. It SHALL NOT limit the number of saved profiles.

#### Scenario: Initial migration and saved profiles
- **GIVEN** a new installation or migration without a saved capacity setting
- **WHEN** settings initialize
- **THEN** the limit is 10 and 100 stopped saved profiles can coexist without consuming capacity.

#### Scenario: Invalid or unauthorized update
- **WHEN** a user requests a capacity change, or an administrator submits zero, a negative number, a fraction, a boolean, or malformed data
- **THEN** the server rejects the request and preserves the previous value.

#### Scenario: Valid value survives restart
- **GIVEN** the administrator saves 12
- **WHEN** FoxProfile or its host restarts or the migration is rerun
- **THEN** every launch path continues to use 12 rather than resetting it to 10.

### Requirement: GC-02 Capacity follows actual profile lifecycle
The system SHALL count each reserved, starting, running, or stopping profile once until its browser session is confirmed ended. Multiple tabs, Viewer connections, noVNC connections, and control clients SHALL NOT multiply a profile's occupancy.

#### Scenario: Starting and stopping profiles remain counted
- **GIVEN** eight running profiles, one reserved launch, and one stopping profile with a live process
- **WHEN** the limit is 10 and another profile requests admission
- **THEN** admission is denied even if an upstream in-memory running counter reports fewer than 10.

#### Scenario: Same browser gains more clients
- **GIVEN** all 10 places are occupied
- **WHEN** an authorized user opens Viewer or noVNC for a running profile or an agent attaches to that run
- **THEN** access is evaluated normally and occupancy stays unchanged.

### Requirement: GC-03 One admission service for every launch path
All browser-creating paths SHALL use the same server-side admission policy, including UI, REST, MCP, batch operations, background work, and profile tools that open a browser. Administrative or trusted service authority SHALL NOT bypass capacity.

#### Scenario: All entry points observe a full installation
- **GIVEN** the shared limit is reached
- **WHEN** each supported UI, API, MCP, administrator, or background path requests a new browser-backed profile session
- **THEN** each request fails before spawning a browser and returns the capacity error.

#### Scenario: Cookie helper opens a stopped profile
- **GIVEN** a cookie import/export operation needs the upstream temporary browser helper
- **WHEN** it opens a stopped profile
- **THEN** it obtains the same profile exclusivity and capacity reservation, uses the configured Camoufox launch policy, and releases occupancy only after confirmed cleanup.
- **AND** only operations already supported against a running profile reuse that run without a second reservation; existing stopped-only cookie constraints remain.

### Requirement: GC-04 Authorization precedes atomic reservation
The system SHALL authorize the request and enforce profile exclusivity before atomically checking and reserving capacity. Reservations SHALL be shared across processes serving the same installation, not held only in a worker's memory.

#### Scenario: Two requests compete for the last place
- **GIVEN** nine places are occupied with a limit of 10
- **WHEN** two independent processes concurrently request different authorized profiles
- **THEN** exactly one obtains a reservation and at most one new browser starts.

#### Scenario: Duplicate or unauthorized request
- **WHEN** two callers request the same profile or a caller requests a forbidden profile
- **THEN** existing same-profile exclusivity prevents duplicate browser sessions, and an unauthorized request neither reserves capacity nor reveals the profile's occupancy.

### Requirement: GC-05 Atomic batch admission
Before a batch launch changes anything, the system SHALL authorize the whole set, deduplicate targets, and atomically reserve all required new places. If authorization or capacity fails, no new profile in that batch SHALL start. No automatic waiting queue SHALL be added.

#### Scenario: Batch cannot fit
- **GIVEN** nine places are occupied with a limit of 10
- **WHEN** a batch requests two distinct new authorized profiles
- **THEN** the whole batch is rejected with neither profile started and no new reservation retained.

#### Scenario: Existing runs and duplicated names
- **GIVEN** a batch includes a running profile, a duplicate target, and one stopped profile
- **WHEN** admission evaluates the batch
- **THEN** existing API semantics for already running profiles are preserved and only one new place is required for the stopped profile.

#### Scenario: A batch contains a forbidden target
- **WHEN** a caller submits an allowed and a forbidden profile together
- **THEN** the entire request is denied before any target is modified, started, or reserved.

### Requirement: GC-06 Launch completion and transport timeouts are distinct
A client wait timeout SHALL NOT release capacity for a launch still in progress. A failed, cancelled, or expired launch SHALL retain its reservation until every browser process it created is confirmed ended; then release SHALL be idempotent.

#### Scenario: Existing API returns accepted while starting
- **GIVEN** the upstream launch wait expires and the browser remains starting
- **WHEN** the API returns an accepted/starting response such as HTTP 202
- **THEN** the reservation remains active and the eventual browser outcome controls its release.

#### Scenario: Failed startup has a surviving process
- **GIVEN** startup failed after creating part of a process tree
- **WHEN** cleanup has not yet confirmed that browser tree ended
- **THEN** the reservation remains counted and the profile cannot be relaunched into another place.
- **AND** repeated cleanup notifications after confirmed termination release it exactly once.

### Requirement: GC-07 Runtime failures after batch admission
If an admitted batch later experiences a runtime launch failure, the system SHALL report per-profile outcomes and clean up failed runs safely. Atomic admission SHALL NOT be represented as a guarantee that all browsers start successfully or as permission to kill successful runs.

#### Scenario: One browser fails after all reservations succeed
- **GIVEN** a batch of three profiles was fully admitted
- **WHEN** one launch fails and two become running
- **THEN** the successful runs remain available, the failed reservation is released after cleanup, and the result identifies the allowed targets' actual outcomes.

### Requirement: GC-08 Confirmed shutdown frees the matching run only
Stopping, deleting, or detecting a crashed browser SHALL release its reservation only after termination is confirmed for the matching run identity. A late callback from an older run SHALL NOT release a replacement run's capacity.

#### Scenario: Stop returns before the OS process exits
- **WHEN** the launcher removes a run from its active dictionary before its process tree has exited
- **THEN** the durable occupancy still counts that run until termination is confirmed.

#### Scenario: Duplicate and stale completion events
- **GIVEN** an old run ended and the same profile now has a new run
- **WHEN** the old cleanup event is delivered again
- **THEN** it cannot free, stop, or alter the new run or its reservation.

### Requirement: GC-09 Recovery precedes new admissions
After manager restart, the system SHALL reconcile reservations with verified live browser sessions before admitting new launches. Unknown liveness SHALL fail closed for new admission until resolved; a cleared memory counter alone SHALL NOT prove termination.

#### Scenario: Live browsers and stale reservations survive a manager crash
- **GIVEN** the manager restarts with both live recorded browser sessions and abandoned pre-spawn reservations
- **WHEN** recovery verifies process identity and liveness
- **THEN** live sessions stay counted, proven stale reservations are removed, and admission opens only after reconciliation.

#### Scenario: Process identifiers have been reused
- **WHEN** a stored PID now belongs to a different process or its start time/run identity does not match
- **THEN** recovery does not attach to or terminate that process as if it were the original browser.

#### Scenario: Parent dies between child spawn and registration
- **GIVEN** a reservation exists and its runner child has been spawned but the parent's in-memory registration has not completed
- **WHEN** the parent crashes
- **THEN** durable child registration or an equivalent verified OS-owned run marker makes any browser-creating child recoverable and counted.
- **AND** the child cannot create a browser before claiming its still-valid run, and a late child cannot spawn after that run was cancelled or reclaimed.

### Requirement: GC-10 Capacity changes preserve running work
Lowering the limit below occupancy SHALL NOT terminate existing sessions. New admissions SHALL remain blocked while occupancy is at or above the saved limit. Raising the limit SHALL permit later launches without restarting current browsers.

#### Scenario: Limit falls from 10 to 6
- **GIVEN** 10 profiles occupy places
- **WHEN** the administrator saves a limit of 6
- **THEN** the 10 sessions keep running and new launches are blocked until occupancy is less than 6.
- **AND** the admin UI explains the temporary restriction before or with the saved result.

#### Scenario: Limit changes during concurrent admission
- **WHEN** a limit update races with a launch reservation
- **THEN** a transactionally ordered decision uses the effective saved limit and cannot admit beyond it except for already admitted sessions preserved by a decrease.

### Requirement: GC-11 Actionable and non-disclosing capacity errors
REST and MCP SHALL expose a recognizable `PROFILE_CAPACITY_EXCEEDED` domain error; the panel SHALL show the configured limit and a retry-after-stop explanation without revealing inaccessible profiles or users. Capacity errors SHALL NOT disable allowed operations on existing runs.

#### Scenario: User encounters a full installation
- **WHEN** admission fails with a configured limit of 12
- **THEN** the UI explains that the shared limit is 12, suggests stopping an accessible profile or contacting an administrator, and reveals no foreign profile names, owners, or secrets.
- **AND** Viewer, permitted stop, and noVNC attachment to that user's current run remain usable.

### Requirement: GC-12 Administrator observes effective occupancy
The existing administrative settings UI SHALL show the configured capacity and authoritative total occupied places, including reservations and launches in progress. Ordinary users SHALL NOT receive global profile rosters or an editable capacity setting.

#### Scenario: Administrator checks a mixed lifecycle
- **GIVEN** five running profiles, one reserved launch, and one starting profile
- **WHEN** the administrator opens settings with a limit of 10
- **THEN** the UI reports seven occupied out of 10 and accepts only valid administrative changes.

