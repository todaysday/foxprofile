## Purpose

Expose a reliable identity for each existing Camoufox run and its virtual display so temporary manual access can reuse the exact browser without changing its profile or creating an extra session.

## ADDED Requirements

### Requirement: VR-01 Reuse the existing virtual-display launch path
On target Linux servers, Camoufox sessions SHALL use `FOXPROFILE_HEADLESS=virtual` from launch. The implementation SHALL extend the existing runner-owned Xvfb path, not nest another display or restart a browser when manual access is requested.

#### Scenario: Existing upstream virtual launch
- **GIVEN** the audited runner creates a `VirtualDisplay` and passes `headless=False` plus its `virtual_display` to Camoufox
- **WHEN** the extension starts a profile
- **THEN** this headed-on-Xvfb mechanism is retained, one display belongs to the run, and no second Xvfb is added.

#### Scenario: Legacy true-headless browser is already running
- **GIVEN** a run has no attachable display
- **WHEN** a user requests noVNC
- **THEN** the system reports manual access unavailable for that run, preserves it, and explains that configured virtual mode applies at a later deliberate launch.

### Requirement: VR-02 Stable profile and unique run identity
Each active run SHALL bind immutable `profile_id`, installation `host_id`, unique `browser_run_id`, and verified process identity to its actual display and X authorization context. Runtime identity SHALL be separate from mutable profile names and reusable slot/display numbers.

#### Scenario: Runtime descriptor is published
- **WHEN** a profile becomes ready
- **THEN** its trusted runtime descriptor identifies the exact host, run, display, namespace, runner/browser process identities, and Xauthority when applicable.
- **AND** privileged paths and control secrets remain server-side rather than in ordinary profile DTOs.

#### Scenario: Display number is reused
- **GIVEN** run A ended and run B uses the same display string
- **WHEN** an old manual request identifies run A
- **THEN** the server rejects the request rather than resolving it to B.

### Requirement: VR-03 Trusted runtime discovery and readiness
Only verified runner/launcher state SHALL establish display bindings. The client SHALL NOT select `DISPLAY`, Xauthority, PID, namespace, VNC address, or arbitrary control ports. Existing control readiness ordering SHALL be preserved.

#### Scenario: Child becomes ready for control and manual attachment
- **WHEN** the runner reports a display and control endpoint
- **THEN** the launcher validates and associates them with the reserved run before reporting that manual attachment is available.
- **AND** the existing `CONTROL` information remains available before `BROWSER_STARTED` is treated as control-ready.

#### Scenario: Client attempts a foreign display override
- **WHEN** a request includes a display, host target, socket path, port, or process value not selected by the server for the authorized run
- **THEN** it is rejected or ignored as an unsupported field and cannot redirect the connection.

### Requirement: VR-04 Distinct runtime and manual resource lifecycles
Browser and display lifetime SHALL follow the profile run. VNC processes and WebSocket lifetime SHALL follow temporary manual sessions. Closing noVNC SHALL NOT kill Camoufox or Xvfb, alter profile data, or release the browser's capacity reservation.

#### Scenario: Manual access closes
- **GIVEN** a running profile with an attached VNC adapter
- **WHEN** the user returns to ordinary control or the manual connection expires
- **THEN** only that manual session's resources are reclaimed and the same browser, display, tabs, and occupancy remain.

#### Scenario: Browser run ends
- **WHEN** the browser run actually terminates
- **THEN** its manual sessions are invalidated and their bridges closed before any display identity can be reused.
- **AND** existing verified display cleanup is retained and capacity is released only after browser termination is confirmed.

### Requirement: VR-05 Display isolation and namespace correctness
Each attachable display SHALL contain only the intended profile's browser session and necessary local display resources. The VNC adapter SHALL share the relevant X socket namespace and authorization context; equal display strings on different hosts or containers SHALL NOT imply identity.

#### Scenario: systemd private temporary directory
- **GIVEN** the deployment uses `PrivateTmp=true`
- **WHEN** a VNC adapter attaches to the runner's Xvfb
- **THEN** it runs with access to that same private socket namespace rather than an unrelated service's `/tmp`.

#### Scenario: Two hosts both use display 101
- **WHEN** an authorized manual session is resolved
- **THEN** its host and run binding select exactly one display; no cross-host display lookup or distributed scheduler is introduced.

### Requirement: VR-06 Preserve profile identity and per-profile exclusivity
Run allocation and manual attachment SHALL preserve the profile's persistent directory, proxy, fingerprint, locale, timezone, and geometry. Existing same-profile locks SHALL remain effective across ordinary launches and helper browsers.

#### Scenario: Different client attaches to an agent-started profile
- **GIVEN** an agent launched an authorized user's profile
- **WHEN** that user opens ordinary Viewer and then noVNC
- **THEN** both attach to the same run with unchanged identity and data, without copying cookies or allocating another browser.

#### Scenario: Same profile is requested through a helper and normal launch
- **WHEN** a cookie helper and a normal launch compete for the same persistent directory
- **THEN** common exclusivity prevents two browser sessions from opening that directory concurrently.

### Requirement: VR-07 Scope manual runtime support without engine regression
Initial noVNC and stealth integration SHALL target Camoufox on ARM64 and x86-64 Linux without a GPU. Existing other-engine operations SHALL retain access controls and capacity accounting; unsupported manual access SHALL be explicit and SHALL NOT silently replace the engine.

#### Scenario: Chrome profile has no supported display
- **WHEN** its authorized user requests manual access
- **THEN** the button is disabled with an explanation or the API returns `MANUAL_UNSUPPORTED_RUNTIME`, and no replacement Camoufox or Chrome browser is launched.
- **AND** existing permitted Chrome profile operations continue to work within the same global capacity limit.

### Requirement: VR-08 Verify resources on both target architectures
Acceptance SHALL measure 1, 5, and 10 running browsers before, during, and after one manual session on each target architecture. The implementation SHALL NOT claim a fixed memory saving, GPU-equivalent fingerprint, or universal ten-browser hardware capacity without evidence.

#### Scenario: Resource acceptance is recorded
- **WHEN** the scenario matrix runs on ARM64 and x86-64 hosts
- **THEN** the report records versions, page/tab workload, peak memory, CPU, input responsiveness, and remaining temporary processes for each case.
- **AND** ordinary operation has zero temporary VNC processes, manual operation at most one, and closing it leaves no accumulating processes or extra browser.

