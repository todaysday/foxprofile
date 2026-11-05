## Purpose

Restrict ordinary users to assigned browser profiles while preserving their complete existing profile functionality and the installation's trusted administration and automation workflows.

## ADDED Requirements

### Requirement: PA-01 Stable profile identity and complete access grants

Each locally managed profile SHALL have an immutable server-generated `profile_id`. Ordinary access SHALL derive from a `UserProfileAccess` relation keyed by user and profile ID; administrator access SHALL be implicit. Every grant SHALL provide complete existing profile rights. Rename SHALL preserve identity and grants. Unassigned profiles SHALL remain inaccessible to ordinary users.

#### Scenario: Assigned user and administrator access a profile

- **GIVEN** an active ordinary user assigned to a profile and an active administrator
- **WHEN** either requests an existing operation on that profile
- **THEN** both are authorized subject to the operation's existing lifecycle constraints
- **AND** the ordinary user's grant does not reduce to an observer or operator permission subset.

#### Scenario: Name is changed or reused

- **GIVEN** assignments attached to a stable profile ID
- **WHEN** the profile is renamed or a different profile later uses a vacated name
- **THEN** the renamed profile retains its grants
- **AND** the different profile does not inherit grants from the old name.

#### Scenario: More than one full-control grant exists

- **GIVEN** multiple users explicitly granted access to the same profile
- **WHEN** they use existing profile operations
- **THEN** each receives the same complete profile rights
- **AND** deleting the profile removes its availability for every assigned user without creating additional permission levels.

### Requirement: PA-02 Consistent creation and automatic assignment

An ordinary user's successful profile creation, native clone, or new native import SHALL durably create the profile and its creator's access grant as one consistent application operation. New profiles SHALL receive fresh local identities. Client-supplied or archive-supplied identities, roles, owners, and assignment recipients SHALL NOT determine local authorization.

#### Scenario: User creates or clones a profile

- **GIVEN** an authenticated ordinary user and, for cloning, an authorized source profile
- **WHEN** a native create or clone operation succeeds
- **THEN** the new profile has a fresh server-generated identity and is immediately assigned to that user
- **AND** other ordinary users cannot see it without a separate administrator grant.

#### Scenario: Imported metadata attempts to grant access

- **GIVEN** a valid native profile archive containing foreign owner, user, role, or profile-ID metadata
- **WHEN** an ordinary user imports it as a new profile
- **THEN** authorization metadata from the archive is ignored or rejected
- **AND** any successful import receives a fresh local identity and only the importing user's automatic grant.

#### Scenario: Profile or grant persistence fails

- **GIVEN** a create, clone, or import operation interrupted between catalog and access persistence
- **WHEN** a save fails or the application restarts
- **THEN** it does not report a fully successful operation or expose a partially committed profile to another user
- **AND** recovery completes or reverses the affected operation before ordinary use resumes.

### Requirement: PA-03 Complete existing panel and profile operations

Assigned users SHALL retain the existing panel and all native controls for their profiles: state, start/stop, Viewer, input, supported navigation/tabs, settings, proxy, fingerprint/environment, cookies, deletion, bulk actions, and native clone/import/export. Existing stopped-profile preconditions SHALL still apply. This change SHALL NOT introduce hidden role-based restrictions or require absent native features.

#### Scenario: User operates an assigned profile through the panel

- **GIVEN** an assigned profile in the lifecycle state required by an operation
- **WHEN** its user inspects state, starts or stops it, edits settings, proxy or fingerprint, imports or exports cookies, or uses supported native profile transfer operations
- **THEN** the panel exposes the existing controls and the server permits the authorized operation
- **AND** explicit configuration edits persist normally.

#### Scenario: Existing runtime precondition is unmet

- **GIVEN** an assigned profile is running and an existing settings operation requires it to be stopped
- **WHEN** its user attempts that operation
- **THEN** the system applies the existing lifecycle restriction
- **AND** it does not add an additional restriction solely because the actor is an ordinary user.

#### Scenario: User deletes an assigned test profile

- **GIVEN** an assigned disposable profile and another unrelated profile
- **WHEN** its user deletes the disposable profile through the native operation
- **THEN** the deletion is permitted subject to native lifecycle requirements
- **AND** the unrelated profile and its data are unaffected.

### Requirement: PA-04 Shared server-side authorization before effects

Every protected entry point SHALL enforce the same authenticated profile policy before reading data, mutating state, launching a process, or issuing a resource. This SHALL cover REST, direct Flet/service calls, Viewer transports, and internal adapters. User-interface hiding and network location SHALL NOT substitute for authorization.

#### Scenario: Authorized call reaches shared services

- **GIVEN** a user authorized for a target profile
- **WHEN** an existing REST or direct-panel service operation resolves that profile
- **THEN** the shared authorization boundary validates the actor and profile before performing the operation.

#### Scenario: Foreign profile is addressed directly

- **GIVEN** user A is assigned only profile A and profile B exists
- **WHEN** user A forges a name, ID, request body, or direct service invocation to read, edit, start, stop, delete, clone, or export profile B
- **THEN** the server denies access before any profile data, process change, file output, or mutation is produced
- **AND** the denial does not disclose profile B's settings or credentials.

#### Scenario: Unauthenticated caller opens a protected surface

- **GIVEN** no valid personal or trusted service authentication
- **WHEN** a caller requests protected panel data, a profile API, Viewer imagery, or a control transport
- **THEN** access is denied before protected content is returned
- **AND** public login or static application resources do not contain protected bootstrap data.

### Requirement: PA-05 Authorization of indirect resources

The server SHALL resolve tab IDs, browser IDs, Viewer sessions, screenshot or stream resources, and export/download references to their authoritative owning profile before authorizing access. A resource identifier or previously issued link SHALL NOT independently confer access. Foreign and unavailable resources SHALL use a consistent non-disclosing response.

#### Scenario: User operates a tab belonging to an assigned profile

- **GIVEN** the server associates a tab or browser resource with an assigned profile
- **WHEN** the user requests a supported tab, screenshot, navigation, or input operation
- **THEN** the operation is authorized against that profile's current grant.

#### Scenario: User guesses another profile's resource identifier

- **GIVEN** a resource belongs to an unassigned profile
- **WHEN** the user submits its tab ID, browser ID, Viewer session ID, stream URL, or export reference
- **THEN** the server rejects access without returning that resource's content
- **AND** an unavailable resource produces the same non-disclosing class of response.

#### Scenario: Resource is replaced after authorization lookup

- **GIVEN** a resource identifier has been resolved to a particular profile and browser instance
- **WHEN** the resource is closed, replaced, or changes ownership before the operation executes
- **THEN** the operation validates the current binding or fails closed
- **AND** it cannot act on a newly bound foreign resource.

### Requirement: PA-06 Server-filtered discovery and associated information

The server SHALL filter ordinary-user profile lists, search, filters, running-profile lists, counters, groups, and existing associated logs to authorized profiles. Responses SHALL exclude unrelated proxy values, cookies, screenshots, fingerprints, and resource identifiers. Global capacity information SHALL follow `global-profile-capacity` without exposing foreign profiles.

#### Scenario: User searches and filters the existing panel

- **GIVEN** user A has profile A while other users have additional profiles
- **WHEN** user A uses profile search, status filters, running lists, groups, counters, or existing profile logs
- **THEN** results and profile-derived counts reflect only profiles accessible to user A
- **AND** group views do not reveal names or membership of inaccessible profiles.

#### Scenario: Search term or aggregate would reveal another profile

- **GIVEN** a search term matches only an inaccessible profile or a group contains inaccessible members
- **WHEN** an ordinary user searches, requests an aggregate, or opens associated logs
- **THEN** the server returns only authorized information
- **AND** client-side filtering is not required to remove foreign records from the response.

### Requirement: PA-07 Whole-request authorization for bulk actions

A bulk profile operation SHALL resolve and authorize every target before causing any effect. If any target is inaccessible or unavailable, the entire bulk request SHALL be rejected before mutations, starts, stops, exports, or reservations occur. Bulk-start capacity admission SHALL additionally follow `global-profile-capacity`.

#### Scenario: Every bulk target is assigned

- **GIVEN** every requested profile is accessible to the actor and native operation preconditions hold
- **WHEN** the actor submits a bulk operation
- **THEN** the operation may proceed after whole-request authorization
- **AND** bulk starts also pass the shared atomic capacity admission.

#### Scenario: Authorized and foreign profiles are mixed

- **GIVEN** user A may access profile A but not profile B
- **WHEN** user A submits a bulk request containing both profiles
- **THEN** the entire request is rejected
- **AND** neither profile is changed, started, stopped, exported, or reserved as a consequence of that request.

#### Scenario: Grant changes while the bulk operation is admitted

- **GIVEN** a bulk request is being admitted while an administrator revokes access to one target
- **WHEN** revocation commits before the bulk operation's authorization decision commits
- **THEN** the request fails authorization as a whole before producing effects
- **AND** implementations serialize or revalidate admission so stale per-item checks cannot bypass that decision.

### Requirement: PA-08 Immediate revocation across open transports

Revoking a profile grant or disabling its user SHALL prevent subsequent protected commands and data delivery, including already-open Viewer and WebSocket connections. Authorization SHALL be rechecked beyond connection establishment, and affected connections SHALL close or cease protected delivery. Revocation alone SHALL NOT stop the browser or impose a new human/agent coordination mechanism.

#### Scenario: Administrator revokes access during manual viewing

- **GIVEN** an ordinary user is viewing and controlling an assigned running profile
- **WHEN** an administrator revokes that grant
- **THEN** subsequent API operations and manual commands are rejected, new protected frames cease, and affected live connections close or lose protected delivery
- **AND** the browser continues for authorized administrators, remaining grantees, or trusted automation.

#### Scenario: Queued command encounters a committed revocation

- **GIVEN** a command has arrived but has not passed the authorization boundary for execution
- **WHEN** the user's grant is revoked before that boundary
- **THEN** the command is rejected even if its transport was previously authenticated
- **AND** reconnecting or reusing an old Viewer or export reference does not restore access.

#### Scenario: Another authorized actor remains connected

- **GIVEN** two authorized actors share a profile through existing supported access paths
- **WHEN** one ordinary actor loses access
- **THEN** only the revoked actor's access is terminated
- **AND** no new dispatcher, agent pause/resume workflow, or human-versus-agent lock is required.

### Requirement: PA-09 Confined native profile transfer

Native profile and cookie imports/exports SHALL be limited to authorized profile data. Ordinary users SHALL use managed uploads/downloads or confined staging instead of arbitrary server paths. New imports SHALL get fresh local IDs; authorized overwrite SHALL preserve the target's identity and grants. Portable exports SHALL exclude local authorization identities, account data, and service secrets.

#### Scenario: User imports or exports an assigned profile

- **GIVEN** an ordinary user authorized for a native transfer operation
- **WHEN** the user uploads supported profile or cookie data or downloads an export
- **THEN** the transfer is confined to managed staging and the authorized profile's data
- **AND** the archive does not expose user accounts, ACL records, local profile IDs, installation secrets, or unrelated profiles.

#### Scenario: User overwrites an authorized existing target

- **GIVEN** the native importer supports overwrite and the chosen target is assigned to the user
- **WHEN** the user explicitly imports into that target and existing lifecycle preconditions hold
- **THEN** the target retains its local profile ID and access grants
- **AND** imported authorization fields cannot change those grants.

#### Scenario: Archive or path escapes its intended scope

- **GIVEN** an ordinary user submits a server `zip_path`, `export_dir`, archive path, symlink, or profile name that resolves outside managed staging or the authorized profile directory
- **WHEN** the transfer is validated
- **THEN** the request is rejected before reading or writing outside the permitted scope
- **AND** the user cannot overwrite an inaccessible existing profile.

### Requirement: PA-10 Profile settings and proxy isolation

Assigned users SHALL read and modify their own profile proxy credentials and existing fingerprint/environment settings. An existing shared proxy library SHALL be scoped or bypassed through direct per-profile configuration so users cannot obtain unrelated proxies. Profile settings SHALL NOT authorize changes to arbitrary server paths, another profile directory, service ports, or installation-wide configuration.

#### Scenario: User updates own proxy and fingerprint settings

- **GIVEN** an assigned profile satisfying the existing edit preconditions
- **WHEN** its user explicitly changes its proxy credentials or fingerprint/environment settings
- **THEN** the values are saved for that profile and available to its authorized controls
- **AND** access assignment or login does not itself regenerate the fingerprint.

#### Scenario: Profile update contains installation fields

- **GIVEN** an ordinary user authorized to edit one profile
- **WHEN** it injects service-port, global-setting, arbitrary-path, foreign-directory, or unrelated proxy-library fields into a profile update
- **THEN** the server rejects the unauthorized fields or request before applying them
- **AND** ordinary profile-edit rights do not become installation administration.

### Requirement: PA-11 Administrator assignment workflow

The existing administrative panel SHALL support finding users and profiles and granting or revoking full profile access using simple controls. Only administrators SHALL modify assignments. Assigning or revoking access SHALL preserve the profile's cookies, site data, fingerprint, proxy configuration, and running browser identity.

#### Scenario: Administrator assigns an existing profile

- **GIVEN** an administrator and an existing unassigned profile
- **WHEN** the administrator locates the user and profile and grants access
- **THEN** the ordinary user can access the full native profile controls
- **AND** stored site logins and profile configuration remain unchanged.

#### Scenario: Ordinary user submits a forged assignment

- **GIVEN** an ordinary user's session
- **WHEN** it directly submits an assignment, revocation, recipient change, or ownership change
- **THEN** the server rejects the administrative operation
- **AND** existing assignments and profile data remain unchanged.

### Requirement: PA-12 Durable identity during catalog operations

Profile catalog writes SHALL report persistence failures. Rename, deletion, import, and identity migration SHALL preserve correct ID-to-data and ID-to-grant bindings through interruption and recovery. A deleted or failed-operation directory containing residual browser data SHALL be quarantined or barred from name/path reuse until cleanup is verified. Incomplete operations SHALL fail closed for affected ordinary access.

#### Scenario: Rename completes durably

- **GIVEN** a profile with local identity, grants, and stored browser data
- **WHEN** its authorized rename succeeds
- **THEN** the new catalog name resolves to the same identity, grants, and browser data after restart
- **AND** no authorization record is reassigned merely by matching a name.

#### Scenario: Save failure interrupts rename or import

- **GIVEN** an authorized catalog operation changes files or in-memory metadata
- **WHEN** durable catalog or access persistence fails
- **THEN** the caller receives a failure or explicit incomplete-operation result
- **AND** recovery reconciles the operation before affected ordinary access is exposed
- **AND** it does not report success while the durable state still represents a different profile.

#### Scenario: Deleted directory retains another user's cookies

- **GIVEN** profile deletion fails to remove some browser data
- **WHEN** a caller attempts to create or import a new profile using that name or directory
- **THEN** the residual directory is quarantined or the reuse is refused until verified cleanup
- **AND** the new profile cannot inherit the previous profile's cookies, site sessions, identity, or access grants.

### Requirement: PA-13 Attach to the existing browser without ownership side effects

An assigned user SHALL attach to the same running browser instance regardless of whether an administrator, the user, or trusted automation started it. Manual access SHALL follow `manual-browser-access` and consume no additional browser slot. Existing duplicate-instance and operation locks SHALL remain effective without a newly introduced human/agent coordinator.

#### Scenario: User opens a profile started by an agent

- **GIVEN** trusted automation or an administrator has started a profile assigned to the user
- **WHEN** that user opens its Viewer or supported manual access
- **THEN** the user sees and controls the same browser instance and existing site session
- **AND** no second browser is launched, fingerprint regenerated, or capacity slot reserved.

#### Scenario: User requests manual access while global capacity is full

- **GIVEN** an assigned profile is already running and the installation has reached its browser capacity limit
- **WHEN** the user opens or reconnects its Viewer or requests a supported manual operation
- **THEN** capacity saturation does not by itself deny access to that running profile
- **AND** ordinary authorization and the existing runtime lifecycle rules still apply.

### Requirement: PA-14 Isolate native browser filesystem and process access

Full browser input, including noVNC chrome and native dialogs, SHALL preserve profile-data isolation. A profile's browser SHALL NOT read sibling profiles, service secrets, extension state, or other processes' private state, nor write outside permitted profile and private runtime/staging paths. The runtime SHALL enforce confinement and sanitize inherited environment; REST input filtering alone SHALL NOT count as enforcement.

#### Scenario: User attempts foreign file access through noVNC

- **GIVEN** harmless canaries exist in another profile, service configuration, extension state, and a same-service-UID process environment
- **WHEN** a user uses browser chrome, file URLs, native open/save dialogs, or a link to attempt reading them or writing outside allowed paths
- **THEN** the native browser cannot reveal the canaries or perform the unauthorized writes, including through filesystem links or other-process `/proc` paths
- **AND** ordinary permitted navigation and the selected profile's persistent browser data continue working.

#### Scenario: Interactive browser examines its own environment

- **GIVEN** the server uses a service/API credential and other privileged environment values
- **WHEN** it spawns the profile's interactive browser and that browser attempts to inspect its own process environment
- **THEN** privileged server credentials are absent from the child's environment and cannot be disclosed through `/proc/self/environ` or equivalent browser-accessible paths.

#### Scenario: Required confinement is unavailable

- **GIVEN** a runtime cannot establish the required native-browser isolation
- **WHEN** it is asked to enable personal full manual access
- **THEN** it fails closed with an actionable runtime error instead of exposing an unrestricted shared-service-UID browser
- **AND** acceptance remains blocked until the boundary is enforced; trusted-users-only is not silently substituted for the required profile isolation.
