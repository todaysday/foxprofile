## Purpose

Register a pool of outbound-connected Ubuntu 24.04 VPS browser execution Workers without introducing additional profile masters or bypassing the current protected FoxProfile installation.

## ADDED Requirements

### Requirement: DW-W01 Single authoritative Master
The system SHALL retain one authoritative Master for profile catalog/data, fingerprints/cookies, users/ACL, global capacity, REST, MCP and web UI. Every Worker SHALL be an execution-only service and SHALL NOT run an independently editable profile catalog, authorization database, public client API or MCP.

#### Scenario: Worker comes online
- **GIVEN** a provisioned VPS without a local master catalog
- **WHEN** its worker service connects
- **THEN** Master registers one execution node while all profile/client operations still use Master.

#### Scenario: Worker tries to take a direct user request
- **WHEN** a user attempts to use a worker as a separate FoxProfile API or browse another profile
- **THEN** no supported public worker service or profile catalog is available.

### Requirement: DW-W02 Authenticated outbound connection
A Worker SHALL initiate outbound authenticated WSS to Master using a revocable per-worker credential, bound to stable worker_id, boot_id and connection generation. The credential SHALL NOT be the shared Master API token or a personal session. A new authenticated connection SHALL fence old connection generations.

#### Scenario: Worker reconnect
- **GIVEN** worker A disconnects while its first WSS connection is stale
- **WHEN** it authenticates a fresh connection
- **THEN** Master invalidates commands on the old connection and accepts only the current generation.

#### Scenario: Invalid identity
- **WHEN** a Worker presents an unknown/revoked credential or mismatched identity
- **THEN** it receives no run assignment or profile content.

### Requirement: DW-W03 Fresh resource telemetry
A Worker SHALL report health, browser readiness, MemAvailable, cgroup-constrained headroom where relevant, staging-disk headroom, running exact generations and draining state in integer bytes. Heartbeat interval SHALL be 10 seconds; telemetry SHALL be stale after 30 seconds without a Master-received authenticated update. Stale telemetry SHALL make Worker unselectable, not prove its processes died.

#### Scenario: Heartbeat becomes stale
- **GIVEN** a Worker was ready at 10:00
- **WHEN** more than 30 seconds pass without a valid update
- **THEN** Scheduler rejects new placements on it and retains ownership of previously known runs.

#### Scenario: Invalid telemetry
- **WHEN** a Worker reports negative or implausible memory/disk values or missing active-run inventory
- **THEN** it cannot be selected until validated data arrive.

### Requirement: DW-W04 Ubuntu 24.04 runtime equivalence
Every ready Worker and local Master runtime SHALL use a deployment-pinned Ubuntu 24.04 browser environment. Admission SHALL require compatible manifest digest and native preflight of engine builds, fonts, display geometry, rendering backend, sandbox and actual browser-visible fingerprint-relevant outputs. Scheduling SHALL NOT maintain per-profile OS selection.

#### Scenario: Different browser build or renderer
- **GIVEN** one node has a different engine hash or verified rendering behavior
- **WHEN** it registers
- **THEN** it is marked incompatible and receives no new profile run.

#### Scenario: Same prepared environment on two VPS
- **WHEN** two Ubuntu 24.04 Workers independently pass the equivalence preflight
- **THEN** either can be selected for an otherwise eligible profile without profile-to-host pinning.

### Requirement: DW-W05 Drain and deployment ownership
External deployment SHALL provision and manage the Worker service. Master SHALL support ready, draining, offline and incompatible status without running SSH or managing VPS creation. A draining Worker SHALL receive no new runs but SHALL be permitted to stop and synchronize existing ones.

#### Scenario: Planned replacement
- **GIVEN** Worker A is running a session
- **WHEN** an administrator marks it draining
- **THEN** the session can finish and sync while no further profile is scheduled there.
