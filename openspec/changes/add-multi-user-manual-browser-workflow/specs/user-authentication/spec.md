## Purpose

Provide individual account authentication and administrative controls while preserving a separate trusted automation identity in the existing FoxProfile application.

## ADDED Requirements

### Requirement: UA-01 Individual accounts and exactly two human roles

The system SHALL identify each human through an individual account with a unique username, a password hash, an active flag, and exactly one role: `admin` or `user`. Human authorization SHALL use the authenticated server-side identity. The system SHALL NOT introduce additional human roles or public self-registration for this change.

#### Scenario: Individual user signs in

- **GIVEN** an administrator has created an active account with role `user`
- **WHEN** its owner supplies valid credentials
- **THEN** the system authenticates that account and returns its server-derived role
- **AND** its profile visibility is determined by `profile-authorization`.

#### Scenario: Client attempts to select administrative identity

- **GIVEN** a signed-in ordinary user
- **WHEN** a request, modified browser state, or UI payload supplies a different user ID, role, owner, or assignment recipient
- **THEN** the supplied values do not replace the authenticated actor or grant additional authority
- **AND** unknown human role values are rejected by account administration.

### Requirement: UA-02 Password protection

The system SHALL store passwords using a maintained password-hashing implementation with an appropriate adaptive password hash and per-password salt. It SHALL NOT persist or log plaintext passwords, expose password hashes to the panel, or log browser-site cookies and service credentials. Administrative password reset SHALL replace the password without revealing the previous one.

#### Scenario: Administrator creates or resets a password

- **GIVEN** an authenticated administrator managing a human account
- **WHEN** the administrator sets a password
- **THEN** only its password hash is persisted as authentication material
- **AND** the password and hash are absent from application logs and account-list responses.

#### Scenario: Invalid credentials are supplied

- **GIVEN** an absent, disabled, or incorrectly authenticated account
- **WHEN** a login attempt is made
- **THEN** no authenticated session is issued
- **AND** the response does not reveal stored password material or whether a supplied password matches a disabled account.

### Requirement: UA-03 Revocable personal sessions

The system SHALL issue expiring, revocable personal sessions, support logout, and require an active account for every protected interaction. Public deployments SHALL use HTTPS. Cookie-based sessions SHALL use secure cookie attributes and server-validated CSRF protection for state-changing requests. Session material SHALL NOT expose the trusted service credential.

#### Scenario: Personal session is used and logged out

- **GIVEN** an active account with a valid personal session
- **WHEN** it requests an authorized operation and then logs out
- **THEN** the authorized operation may proceed
- **AND** subsequent API, Viewer, or control requests using the logged-out session are rejected.

#### Scenario: Expired or invalidated session remains in an open panel

- **GIVEN** an expired or revoked session in an already-open panel
- **WHEN** the panel requests profile data, a screenshot, a stream, or a control action
- **THEN** the server denies the request and terminates affected long-lived access
- **AND** cached client role or login state cannot restore access.

#### Scenario: Cross-site state change uses an otherwise valid cookie

- **GIVEN** a deployment authenticating the browser through cookies
- **WHEN** a state-changing request lacks valid CSRF evidence
- **THEN** the server rejects the request before any profile, account, assignment, or configuration change.

### Requirement: UA-04 Administrative account lifecycle

Only an active administrator SHALL create accounts, set passwords, change human roles, or disable accounts. Disabling an account, resetting its password, or changing its role SHALL invalidate its existing personal sessions. Disabling an account SHALL preserve its profile data and SHALL NOT stop browser processes solely because the account was disabled.

#### Scenario: Administrator provisions an ordinary account

- **GIVEN** an active administrator
- **WHEN** the administrator creates a `user` account
- **THEN** the account has no access to existing profiles until explicitly assigned
- **AND** the administrator can subsequently assign profiles through the existing panel.

#### Scenario: Account is disabled while its Viewer is open

- **GIVEN** a user with multiple personal sessions and an open Viewer
- **WHEN** an administrator disables the account
- **THEN** all of its sessions cease to authorize API requests, streamed data, and controls
- **AND** stored browser profiles, site logins, and running browsers remain available to authorized administrators or trusted automation.

#### Scenario: Ordinary user calls an account-management endpoint

- **GIVEN** a signed-in ordinary user
- **WHEN** it directly requests account creation, password reset, role change, or disablement
- **THEN** the server rejects the operation before changing account state.

### Requirement: UA-05 Last active administrator protection

The system SHALL prevent disabling or demoting the last active administrator. The check and account change SHALL be performed as one consistent operation so concurrent administrative changes cannot leave the installation without an active administrator.

#### Scenario: Another active administrator remains

- **GIVEN** two active administrators
- **WHEN** one is disabled or changed to `user`
- **THEN** the change may succeed while the other remains an active administrator.

#### Scenario: Last administrator is targeted

- **GIVEN** exactly one active administrator
- **WHEN** a request attempts to disable or demote that account
- **THEN** the system rejects the request and preserves its active administrative role.

#### Scenario: Concurrent demotions would remove every administrator

- **GIVEN** two active administrators
- **WHEN** concurrent requests attempt to demote both
- **THEN** at most one succeeds
- **AND** the other fails after checking the effective account state.

### Requirement: UA-06 Separate trusted automation identity

The existing trusted agent/API/MCP credential SHALL authenticate a service principal distinct from personal sessions. That principal SHALL retain installation-wide profile operations and obey `global-profile-capacity`. Personal requests SHALL retain their own identity through internal calls and SHALL NOT inherit service authority from a proxy, loopback connection, or shared client.

#### Scenario: Trusted automation continues its existing workflow

- **GIVEN** a configured trusted service credential and an existing profile
- **WHEN** an authenticated agent performs an existing API or MCP operation
- **THEN** the operation retains its trusted installation-wide access subject to existing lifecycle rules and the global capacity limit
- **AND** no ordinary-user assignment is required for that service principal.

#### Scenario: Ordinary request traverses an internal service call

- **GIVEN** an ordinary user's personal request
- **WHEN** the application forwards or adapts it through an internal API, loopback route, or direct service call
- **THEN** the original user's profile scope remains in force
- **AND** no service token is inserted to elevate the request.

#### Scenario: Legacy shared token is present during migration

- **GIVEN** an installation using one shared token before this change
- **WHEN** personal account authentication is enabled
- **THEN** trusted automation may continue using the service credential
- **AND** ordinary panel users receive personal sessions instead of that credential.

### Requirement: UA-07 Administrative settings and secret isolation

Only administrators SHALL access user administration, assignment administration, installation-wide settings, and existing MCP or service-credential configuration controls. Ordinary panel HTML, JavaScript, bootstrap state, APIs, browser storage, logs, and exports SHALL NOT disclose service credentials or credentials belonging to unrelated profiles. UI visibility SHALL follow server-derived permissions.

#### Scenario: Administrator uses existing management controls

- **GIVEN** an active administrator
- **WHEN** the administrator opens the existing panel
- **THEN** all profiles and authorized account, assignment, and installation settings are available
- **AND** existing MCP or credential controls remain administrator-only without requiring a new credential editor.

#### Scenario: Ordinary user inspects hidden controls or direct APIs

- **GIVEN** an ordinary user
- **WHEN** it inspects panel resources, client storage, bootstrap responses, or directly invokes a global-settings or credential endpoint
- **THEN** administrative functions are unavailable and the server denies their operations
- **AND** shared service keys and unrelated profile credentials are not disclosed.

### Requirement: UA-08 Protected initial administrator bootstrap

The installation SHALL provide a controlled first-administrator initialization through the existing setup mechanism or a documented one-time administrative command. After an active administrator exists, this initialization SHALL NOT remain an unauthenticated public path for creating or replacing an administrator.

#### Scenario: First administrator is initialized

- **GIVEN** an installation with no human administrator and authorized access to its setup mechanism
- **WHEN** the one-time initialization completes
- **THEN** an active administrator can sign in and access all pre-existing profiles.

#### Scenario: Bootstrap is retried remotely after initialization

- **GIVEN** an initialized installation with an active administrator
- **WHEN** an unauthenticated caller attempts to repeat bootstrap or replace the administrator
- **THEN** the request is rejected and existing accounts are preserved.

### Requirement: UA-09 Authentication migration and durable access state

Migration SHALL preserve profiles, browser-site sessions, proxy/fingerprint configuration, and trusted automation. Accounts, revocation state, and assignments SHALL survive restarts. Unassigned pre-existing profiles SHALL remain accessible to administrators and the trusted service, but not ordinary users. The rollout SHALL document backup and staging verification before production migration.

#### Scenario: Existing installation is migrated and restarted

- **GIVEN** a backup of application metadata and closed browser-profile directories and a staging-verified migration
- **WHEN** accounts and assignments are migrated and the application restarts
- **THEN** administrators still see existing profiles and ordinary users see only their assigned profiles
- **AND** existing browser-site logins, settings, and trusted API/MCP operation remain intact
- **AND** sessions revoked before restart do not become valid again.

#### Scenario: Migration or metadata persistence is interrupted

- **GIVEN** a migration or identity/access metadata update that has not completed durably
- **WHEN** the application recovers after an interruption
- **THEN** it reports or reconciles the incomplete operation before exposing affected resources
- **AND** it does not grant ordinary users access to unassigned profiles or silently accept an incomplete administrative change.
