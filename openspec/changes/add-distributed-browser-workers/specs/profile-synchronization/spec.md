## Purpose

Keep the main profile and fingerprint authoritative on Master while allowing remote browsers to modify their own isolated persistent session data and commit those changes safely on normal stop.

## ADDED Requirements

### Requirement: DW-S01 Master-owned identity and canonical revision
Each profile SHALL have one canonical Master browser-data revision and persisted fingerprint/persona identity. Master SHALL initialize a never-run engine fingerprint once before remote export. Worker SHALL accept the existing identity unchanged and SHALL NOT generate or reset fingerprint/persona merely because the host changed.

#### Scenario: First remote launch
- **GIVEN** a new Camoufox profile has no fingerprint file
- **WHEN** Master prepares a remote snapshot
- **THEN** Master creates and stores the identity exactly once before assigning a Worker, which consumes the same saved identity.

#### Scenario: Missing fingerprint on Worker
- **WHEN** a transferred snapshot lacks required fingerprint/persona bytes or they do not match the Master manifest
- **THEN** Worker refuses to start rather than generating a different identity.

### Requirement: DW-S02 Complete, exclusive startup snapshot
Master SHALL create a checked immutable snapshot of a stopped profile's browser data, launch configuration and required identity files, tied to its profile_id/run_id/base_revision. It SHALL exclude Master users, ACL, API tokens, leases and unrelated host data. Snapshot admission SHALL share locks with cookie-helper, import, delete and other stopped-profile mutations.

#### Scenario: Concurrent cookie import and remote launch
- **WHEN** both operations target the same profile concurrently
- **THEN** only one holds the exclusive data operation and the other cannot read or change inconsistent files.

#### Scenario: Incomplete archive
- **WHEN** a download is interrupted or a member is unsafe, oversized or hash-mismatched
- **THEN** Worker does not spawn a browser or modify Master canonical profile data.

### Requirement: DW-S03 Persist complete state after normal stop
After a normal stop the Worker SHALL confirm the browser tree ended, package all allowlisted modified persistent browser data (not just cookies), upload it via worker-initiated authenticated HTTPS and await a Master commit receipt. Master SHALL verify owner/boot/run/base_revision, required identity hashes, bounded sizes and archive content before replacing the last known-good revision.

#### Scenario: Cookies, local storage and tabs change
- **GIVEN** revision 12 opened on Worker B
- **WHEN** the browser updates cookies, local storage and tabs then cleanly stops
- **THEN** Master commits revision 13, and the next eligible launch sees those updated values with identical fingerprint.

#### Scenario: Worker attempts changed identity
- **WHEN** returned data modifies fingerprint/persona unexpectedly
- **THEN** the upload is rejected; revision 12 remains intact and the run is marked sync-failed/recovery-required.

### Requirement: DW-S04 Atomic idempotent commit and recovery
Canonical promotion SHALL be crash-recoverable across the filesystem and existing SQLite records. A successful exact-run commit SHALL advance revision only once and produce a durable receipt. A repeated identical upload SHALL return that receipt; an old generation, wrong base revision or changed digest SHALL never overwrite a newer revision.

#### Scenario: Ack lost after successful commit
- **GIVEN** Master committed revision 13 but Worker did not receive the response
- **WHEN** Worker retries the same exact upload
- **THEN** Master returns the original receipt with revision 13, not revision 14.

#### Scenario: Master crashes during promotion
- **WHEN** Master restarts after staging/promotion was interrupted
- **THEN** recovery restores either a verified last-good canonical revision or the fully verified new one, never a half-written profile.

### Requirement: DW-S05 Metadata and data separation
A Worker's return upload SHALL affect only the designated browser-data snapshot and its canonical revision. It SHALL NOT import worker-supplied Master profile names, grants, OS policy, authority or stale metadata over independent authorized Master edits. Running remote data SHALL not be mutated by stopped-only helper operations.

#### Scenario: Master changes allowed profile metadata after launch
- **WHEN** the Worker later uploads its browser-data copy
- **THEN** the latest authorized Master metadata remains authoritative rather than reverting to the snapshot's stale copy.

### Requirement: DW-S06 Loss may discard unsaved changes, never canonical data
Network or Worker failure SHALL preserve the most recently verified canonical Master revision. If remote session changes cannot be safely recovered, their loss SHALL be reported and an unsafe late upload SHALL NOT silently merge them. No continuous checkpointing or cookie-by-cookie merge is required.

#### Scenario: Worker dies with uncommitted cookies
- **GIVEN** Master holds revision 12 and Worker has changed cookies but not committed
- **WHEN** Worker disconnects and its data cannot be recovered
- **THEN** revision 12 stays intact and the run remains blocked until stop proof, with unsaved changes visibly reported.
