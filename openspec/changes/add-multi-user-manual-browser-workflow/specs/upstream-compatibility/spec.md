## Purpose

Keep the requested fork maintainable as FoxProfile evolves by isolating extensions, minimizing upstream edits, and detecting compatibility or security regressions before an upstream update is deployed.

## ADDED Requirements

### Requirement: UC-01 Extension-first implementation
New domain rules, DAOs, DTOs, manual-session code, and assets SHALL reside in cohesive extension modules. Existing FoxProfile code SHALL receive small explicit integration hooks. The change SHALL NOT replace the panel, duplicate whole upstream modules, vendor modified browser engines, or rely on runtime monkey-patching.

#### Scenario: Reviewer inspects the fork diff
- **WHEN** implementation is reviewed against the pinned upstream commit
- **THEN** new behavior is isolated, each modified upstream file has a recorded hook purpose, and unrelated formatting, renames, dependency upgrades, or broad refactors are absent.

#### Scenario: A proposed shortcut patches private library internals
- **WHEN** an implementer considers copying an upstream class or patching a private Camoufox/Flet symbol at runtime
- **THEN** the implementer uses an explicit narrow adapter/hook instead, or records a concrete blocker and a tested minimal alternative before proceeding.

### Requirement: UC-02 Pragmatic architecture priority
Implementation decisions SHALL follow `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY`. Domain rules SHALL be separate from storage and transport; DAO/DTO boundaries SHALL be introduced where they serve real responsibilities, without mandatory ceremonial layers or a general-purpose framework.

#### Scenario: Storage or HTTP behavior changes
- **WHEN** the profile capacity or access policy is tested
- **THEN** its domain rules can be exercised without launching a web server or browser, while DAO and transport adapters have focused integration checks.

#### Scenario: Abstract reuse conflicts with simple responsibilities
- **WHEN** removing small duplication would require a universal manager, policy engine, event bus, or speculative plugin platform
- **THEN** the implementation keeps the smaller cohesive design unless an existing requirement demonstrates the abstraction is necessary.

### Requirement: UC-03 Pin and record compatibility evidence
The fork SHALL record the tested upstream commit, extension schema version, runtime dependency versions relevant to its hooks, and the finite integration surface. Compatibility SHALL be asserted by executed contract checks, not by a claim that arbitrary future upstream versions work.

#### Scenario: Baseline is recorded before implementation
- **WHEN** the implementation checkout is prepared
- **THEN** its actual commit is compared with `530b993ff3a7d211aaa0904fba0817b77c75d4c9`, deviations are audited, and the compatibility manifest records the real tested revision rather than assuming the attachment describes all versions.

### Requirement: UC-04 Detect new bypass surfaces after an update
Compatibility checks SHALL inventory protected API routes, direct service/UI actions, launch paths, and profile-tool paths against their policy classification. Unclassified additions SHALL fail review/CI, and protected service boundaries SHALL reject calls missing an authenticated principal or admission context.

#### Scenario: Upstream adds a new profile export route
- **WHEN** an update candidate introduces the route without a policy classification and authorized service call
- **THEN** the inventory gate fails and deployment is blocked until it is covered by the common access policy and targeted negative tests.

#### Scenario: Upstream adds a background browser launcher
- **WHEN** the update candidate attempts to spawn a profile without the shared admission context
- **THEN** the integration contract rejects the path or its regression check fails before deployment; a new route is not silently treated as trusted.

### Requirement: UC-05 Update candidates are tested in isolation
Upstream updates SHALL be merged into a disposable candidate branch/worktree and tested before promotion. Checks SHALL include upstream tests, extension domain tests, adapter contracts, authorization/capacity races, noVNC lifecycle, and migration on copied data. No unattended deployment of upstream HEAD SHALL be added.

#### Scenario: Update changes runner readiness or profile serialization
- **WHEN** the candidate fails the corresponding adapter contract or migration smoke test
- **THEN** the production checkout and data remain unchanged and the candidate receives a focused compatibility fix before retesting.

### Requirement: UC-06 Preserve persistence and rollback boundaries
The extension SHALL preserve existing browser data and upstream profile formats wherever feasible, with versioned additive extension data and idempotent migrations. It SHALL NOT replace the upstream storage system solely to host new access/capacity state, and SHALL document a tested backup/restore path for code and data together.

#### Scenario: Migration is interrupted or repeated
- **WHEN** a migration or cross-store catalog operation is retried after interruption
- **THEN** identity and assignments remain consistent, pending objects stay inaccessible, no profile becomes public, and existing cookies/fingerprints are preserved.

#### Scenario: Restore is required after a failed update
- **WHEN** the operator rolls back an incompatible schema or deployment
- **THEN** they restore a matched code/config/data snapshot using the documented procedure rather than running old upstream code against extension-protected production data without authentication.

### Requirement: UC-07 Maintain a finite compatibility adapter
Upstream-dependent calls and representations SHALL be confined to a documented adapter boundary. The implementation SHALL preserve existing public service/API behavior where compatible with the requested isolation, and expose deliberate behavior changes such as authenticated access and atomic bulk admission explicitly.

#### Scenario: Upstream renames a service method
- **WHEN** a new revision changes a method used by an extension adapter
- **THEN** the adapter contract identifies the mismatch and the fix is localized without duplicating access or capacity logic in every route.

### Requirement: UC-08 Safe activation and packaging
Extension activation SHALL be explicit for the target server deployment and fail startup if required security hooks or migrations are unavailable. Missing extension imports or invalid schema SHALL NOT silently fall back to an unauthenticated single-token panel.

#### Scenario: Enabled extension cannot initialize
- **WHEN** a required DAO, migration, adapter contract, or security hook fails during startup
- **THEN** protected server startup fails with a redacted operator error and does not expose a partially protected panel or API.

### Requirement: UC-09 Separate final deployment slice
Implementation and offline verification SHALL finish with a concrete immutable handoff. All real-server deployment, operational migration and live acceptance SHALL belong to a separate agent in final slice 9; earlier slices SHALL prepare but not execute that work.

#### Scenario: Implementation agent completes local work
- **WHEN** slices 1 through 8 are finished
- **THEN** the agent supplies the tested commit, artifacts, configuration, migration/rollback tooling and offline evidence, leaving slice 9 unchecked and live results marked NOT RUN.

#### Scenario: Separate agent accepts the real deployment
- **WHEN** the deployment agent executes slice 9
- **THEN** it verifies the immutable handoff, performs deployment and live tests, and records actual outcomes before the change can be considered fully accepted or archived.

