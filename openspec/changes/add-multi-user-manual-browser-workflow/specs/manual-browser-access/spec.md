## Purpose

Add temporary, authenticated noVNC input inside the existing FoxProfile control screen while preserving the selected browser run, normal panel controls, profile access boundaries, and the user's independent conversation with an agent.

## ADDED Requirements

### Requirement: MB-01 Embedded manual control

The existing profile control screen SHALL offer an "Open in noVNC" action above its image area for supported Camoufox runs. The action SHALL replace that area with an interactive noVNC view and a "Back" action, without requiring a new tab, another profile selection, or a replacement management panel. Unsupported engines SHALL be identified without altering their run.

#### Scenario: Open noVNC from the ordinary control screen
- **GIVEN** an authorized user is viewing a running profile in the existing control screen
- **WHEN** the user selects "Open in noVNC"
- **THEN** the same screen displays the selected profile through noVNC
- **AND** the profile identity and "Back" action remain visible.

#### Scenario: No running browser is available
- **GIVEN** the selected profile has stopped before the action is processed
- **WHEN** the user requests manual access
- **THEN** the server reports that no current browser run is available
- **AND** the action does not silently launch a browser or redirect to another profile.

#### Scenario: Current engine cannot provide the supported display
- **GIVEN** a Chrome or other unsupported run is selected
- **WHEN** the user views its controls or directly requests a manual session
- **THEN** the UI disables or explains the unsupported noVNC action and the server returns a recognizable unsupported-engine error
- **AND** normal authorized controls remain available without restarting, switching engines, or reconfiguring that browser.

### Requirement: MB-02 Attach to the current browser

Manual access SHALL attach to the selected profile's existing browser run and existing virtual display. Creating or closing manual access SHALL NOT restart the browser, recreate its tabs, copy profile data, change its identity, or consume an additional active-profile place. Ordinary Viewer access SHALL remain available at the profile limit.

#### Scenario: Same run before and after manual access
- **GIVEN** the profile has an active browser run with known tabs and persistent profile data
- **WHEN** the user opens noVNC, performs a manual action, and returns
- **THEN** `browser_run_id` and the browser process identity are unchanged
- **AND** existing tabs, login state, proxy, fingerprint, and saved geometry are preserved.

#### Scenario: Manual access at full browser capacity
- **GIVEN** all configured active-profile places are occupied and the user can access an already running profile
- **WHEN** the user opens its ordinary Viewer and then noVNC
- **THEN** both operations succeed subject to manual-session availability
- **AND** the active-profile count does not increase.

### Requirement: MB-03 One temporary manual session

The system SHALL allow at most one active, starting, or closing manual VNC session per managed instance, enforced atomically across its server processes. Duplicate opens SHALL reuse the caller's valid session or return a recognizable conflict. An occupied session SHALL NOT be silently reassigned; any busy response SHALL conceal profiles the caller cannot access.

#### Scenario: Duplicate open for the same run
- **GIVEN** an authorized caller has a starting or active session for a browser run
- **WHEN** the caller submits another open request for that run
- **THEN** the server returns the existing authorized session or a duplicate-session conflict
- **AND** no second VNC server or independent controlling session is created.

#### Scenario: Concurrent opens for different profiles
- **GIVEN** no manual session exists
- **WHEN** two server processes concurrently attempt to open manual sessions for different browser runs
- **THEN** at most one request reserves and creates a manual session
- **AND** the other request receives a manual-session-busy response without switching the winning session's display.

#### Scenario: Busy profile belongs to someone else
- **GIVEN** a manual session is occupied by a profile the requesting user cannot access
- **WHEN** the user requests another manual session
- **THEN** the response indicates that manual access is busy without exposing the occupied profile or user
- **AND** an authorized administrator may receive the occupied profile's identity.

### Requirement: MB-04 Bind access to a specific run

Each manual session SHALL bind its owner, `host_id`, `profile_id`, and `browser_run_id` to the server-resolved display and runtime namespace. The server SHALL recheck this binding on open, connection, reconnection, and close. A reused display number or slot SHALL NOT identify a run. Clients SHALL NOT choose arbitrary VNC targets, ports, displays, or Xauthority paths.

#### Scenario: A display number is reused
- **GIVEN** an old session was bound to run A on a display later assigned to run B
- **WHEN** a client reconnects with the old session or attempts to close it
- **THEN** the server rejects stale access or completes an idempotent old-session close
- **AND** it neither connects to nor terminates run B's manual session.

#### Scenario: Equal display strings in different namespaces
- **GIVEN** two hosts or containers both have a display named `:101`
- **WHEN** the server opens manual access for one recorded browser run
- **THEN** the VNC process uses that run's host, namespace, socket, and authorization context
- **AND** the identical display string on the other host or namespace cannot be selected accidentally.

#### Scenario: Client attempts target injection
- **GIVEN** the client submits a profile identifier together with an arbitrary display, VNC address, port, or Xauthority path
- **WHEN** the manual-session API validates the request
- **THEN** unsupported targeting fields are rejected
- **AND** the backend derives the permitted target only from its current runtime record.

### Requirement: MB-05 Authorize the complete session lifecycle

Manual-session creation, state retrieval, closure, and WebSocket use SHALL require an authenticated principal and current access to the bound profile. Session identifiers SHALL NOT grant access by themselves. Temporary access SHALL be bound to the principal and run, expire or be revoked, and stop carrying input and images after session closure or access revocation.

#### Scenario: Foreign profile or guessed session identifier
- **GIVEN** a user lacks access to another user's profile
- **WHEN** the user directly requests its manual session, supplies a guessed session identifier, or attempts its WebSocket
- **THEN** the server denies the operation without returning profile data, pixels, credentials, or a usable stream.

#### Scenario: Permissions change during an established connection
- **GIVEN** a user is connected to an authorized manual session
- **WHEN** the user is disabled, logged out, or loses access to the profile
- **THEN** the session's access is revoked and its WebSocket stops forwarding images and input
- **AND** only temporary manual-session resources are released; the browser remains available to authorized principals.

#### Scenario: Old temporary access is replayed
- **GIVEN** a manual session has closed or its browser run has ended
- **WHEN** a client reuses the old session's temporary access material
- **THEN** the WebSocket handshake is denied even if the same numeric display is now in use
- **AND** creating a new session requires current authorization.

### Requirement: MB-06 Protect HTTP and VNC transport

Public manual access SHALL use the authenticated HTTPS/WSS gateway. The gateway SHALL connect to the VNC adapter only through a private connected channel inaccessible to untrusted local processes; no backend listener is permitted. Any backend authentication SHALL terminate server-side, and backend credentials SHALL never reach the browser/noVNC client. Cookie-authenticated operations SHALL enforce Origin and state-change protections.

#### Scenario: Browser crosses an untrusted origin
- **GIVEN** a user has a valid panel session
- **WHEN** another origin attempts a manual-session mutation or unauthorized WebSocket handshake
- **THEN** the server rejects the request according to its CSRF and Origin policy
- **AND** no usable VNC connection is created.

#### Scenario: Inspect the externally reachable surface
- **GIVEN** manual access is active in the deployed environment
- **WHEN** its exposed listeners, client URLs, and application or proxy logs are inspected
- **THEN** only the authorized HTTPS/WSS path is publicly reachable
- **AND** direct VNC ports, untrusted-local access to the backend channel, and backend credentials in the client configuration or traffic are absent.
- **AND** persistent access secrets are absent from ordinary logs.

### Requirement: MB-07 Constrain the VNC adapter

The manager SHALL launch the replaceable adapter with structured arguments, readiness checks, and access limited to the recorded display and gateway-owned private connected channel. It SHALL not expose a backend listener to untrusted local processes, pass backend credentials to the client, concatenate user data into shell commands, disable X11 authorization globally, expose neighboring displays, enable unnecessary file transfer, or install a full desktop environment solely for this feature.

#### Scenario: Attach with the recorded X11 authorization
- **GIVEN** the current browser run uses an isolated display and Xauthority context
- **WHEN** the temporary VNC adapter starts
- **THEN** it receives access only to that display through the recorded context
- **AND** it communicates only through the gateway-owned private connected channel, which an untrusted local process cannot use
- **AND** no backend credential is sent to the browser or noVNC client
- **AND** global X11 access control and neighboring displays remain unchanged.

#### Scenario: Adapter startup fails
- **GIVEN** the adapter binary is missing, cannot authenticate to X11, or fails its readiness check
- **WHEN** manual-session creation is attempted
- **THEN** the server returns an actionable manual-access error and cleans up partial VNC resources while leaving the browser and its display running
- **AND** the failed manual-session reservation stays occupied while any partial VNC child is live and is cleared only after its termination is confirmed.

### Requirement: MB-08 Full input with local scaling

The embedded noVNC client SHALL support mouse press, hold, movement, release, scrolling, and keyboard input. It SHALL use `scaleViewport=true`, `resizeSession=false`, and `viewOnly=false`. Resizing the user's viewport SHALL scale presentation locally without changing the remote display, browser window, or profile geometry.

#### Scenario: Continuous drag and keyboard input
- **GIVEN** an ordinary test page contains a draggable control and a text field
- **WHEN** the user presses, holds, drags, releases, scrolls, and types through noVNC
- **THEN** the page receives the corresponding continuous pointer and keyboard actions
- **AND** the interaction is not emulated by a sequence of isolated REST clicks.

#### Scenario: A phone rotates or its viewport changes
- **GIVEN** noVNC is connected with a saved remote geometry
- **WHEN** the client viewport changes size or orientation
- **THEN** the image scales locally and pointer coordinates still target the displayed content correctly
- **AND** the remote display, browser geometry, and page-visible screen configuration remain unchanged.

### Requirement: MB-09 Suspend only duplicate panel input

While noVNC occupies the control screen, that screen SHALL suspend its ordinary REST mouse and keyboard handlers and hidden screenshot refresh. Returning SHALL restore those handlers and refresh the normal image. This local UI transition SHALL NOT introduce an agent pause service or a new server-wide browser-action interlock.

#### Scenario: One gesture has one input path
- **GIVEN** the screen is in noVNC mode
- **WHEN** the user clicks or types in the noVNC view
- **THEN** the action is forwarded through VNC only
- **AND** the screen neither sends a duplicate REST input event nor continues refreshing its hidden ordinary screenshot view.

#### Scenario: Ordinary panel control returns
- **GIVEN** the manual session has closed successfully
- **WHEN** the normal control image is restored
- **THEN** its existing REST input handlers are re-enabled exactly once
- **AND** the existing API/MCP authorization and control behavior remains in effect without a new pause or resume gate.

### Requirement: MB-10 Back closes only manual access

The "Back" action SHALL revoke manual input, close the connection, release temporary VNC resources, and restore the ordinary control screen with a fresh image. It SHALL preserve the browser and its display. Closure SHALL be idempotent and bound to the named manual session, including when open and close requests overlap.

#### Scenario: Return while keys or buttons may be held
- **GIVEN** a user is in noVNC mode and may have pressed a key or mouse button
- **WHEN** the user selects "Back"
- **THEN** held input is released before the VNC connection is disposed where the run remains reachable
- **AND** temporary VNC resources are released, the browser and display stay alive, and ordinary controls return with a fresh image
- **AND** the next ordinary Playwright click is not treated as a continuation of a drag or modifier-key gesture.

#### Scenario: Duplicate close cannot close a newer session
- **GIVEN** session A is closed and a later session B is active
- **WHEN** a delayed or repeated close request names session A
- **THEN** the server safely reports A as closed
- **AND** session B, its browser, and its display are unaffected.

#### Scenario: Close races with VNC startup
- **GIVEN** a manual session is still starting
- **WHEN** its authorized caller closes it before startup completes
- **THEN** the close cancels or drains creation and cleans up the temporary resources
- **AND** a late readiness result cannot reopen that closed session or enable input.

### Requirement: MB-11 Recover from connection loss safely

Tab closure, network loss, session expiry/timeout, and gateway failure SHALL revoke access and verify cleanup within 60 seconds of the trigger. Detection, reconnect grace, input release, helper termination, and helper exit count toward that limit. Reconnect SHALL reauthorize the same run; ended sessions require new authorization. Release held input where reachable; do not control the agent. If exit is unverified, keep the lease occupied, block conflicts, and report blocked recovery.

#### Scenario: Network loss during a held gesture
- **GIVEN** the user loses the connection while holding a mouse button or key
- **WHEN** the injected network loss starts the end-to-end deadline
- **THEN** the session stops accepting input and its temporary resources are cleaned up
- **AND** dead-peer detection, any supported reconnect grace, and confirmed helper cleanup all finish within 60 seconds total
- **AND** held input is released on the surviving browser so the next ordinary click behaves normally
- **AND** no agent message, success event, or resume command is generated.

#### Scenario: Tab closure, session expiry, or timeout
- **GIVEN** a manual session is active
- **WHEN** its client tab closes or its session expires/times out
- **THEN** the same 60-second deadline starts at that trigger and access is revoked
- **AND** cleanup, including any supported reconnect grace, completes within the deadline or remains blocked until helper exit is confirmed.

#### Scenario: Cleanup cannot be confirmed by the deadline
- **GIVEN** the VNC helper ignores termination or its exit cannot be verified after any cleanup trigger
- **WHEN** 60 seconds elapse from that trigger
- **THEN** the server reports cleanup as blocked and keeps the manual lease occupied
- **AND** a conflicting manual session remains denied until helper termination is confirmed
- **AND** the server does not claim that cleanup succeeded.

#### Scenario: Reconnect after temporary loss
- **GIVEN** the user reconnects after a network interruption
- **WHEN** the server checks the current principal, profile permissions, session state, and browser-run binding
- **THEN** it reconnects only to that still-valid session and run, or requires a new authorized session if the old one ended
- **AND** it never falls back to another run that happens to use the previous display.

#### Scenario: Gateway restart leaves stale manual resources
- **GIVEN** the gateway or manual-session manager restarts during a session
- **WHEN** it recovers its runtime state after an injected gateway failure
- **THEN** it revokes stale access and reconciles or removes owned temporary VNC resources before accepting a conflicting session
- **AND** this cleanup is confirmed within 60 seconds from the failure or the lease stays blocked with a recovery error
- **AND** it does not kill browser/display resources or an unrelated process whose numeric PID was reused.

### Requirement: MB-12 Keep the agent outside noVNC integration

Manual-session lifecycle actions SHALL NOT notify an agent, emit completion events, detect CAPTCHA success, alter agent task state, or pause/resume automation. This feature SHALL NOT add help-request IDs, `/complete`, agent adapters, or a Pause/Resume subsystem. The user SHALL remain free to continue in the ordinary panel and separately tell the waiting agent when ready.

#### Scenario: Back has the same meaning with or without an agent
- **GIVEN** no agent is present, or an agent is independently waiting for the user
- **WHEN** the user opens noVNC and returns to the ordinary screen
- **THEN** the browser-view transition behaves identically in both cases
- **AND** no agent-specific identifier, callback, message, pause request, or resume operation is required or created.

#### Scenario: A challenge remains unresolved
- **GIVEN** a challenge or other manual task remains on the page
- **WHEN** the user returns, disconnects, or lets the session expire
- **THEN** FoxProfile performs only its manual-session lifecycle work
- **AND** it neither declares success nor closes an agent task or sends "ready" on the user's behalf.

### Requirement: MB-13 Preserve other profiles and existing control rights

A manual session SHALL affect only its bound run's temporary access resources. Other running profiles SHALL retain their existing control paths and continue working. Existing authorized stop/delete operations SHALL retain their semantics; when the bound browser ends, all associated manual access SHALL be invalidated and cleaned up without being transferred to a replacement run.

#### Scenario: Neighboring profiles remain operational
- **GIVEN** several profiles are running and one has a manual session
- **WHEN** that manual session opens, closes, or fails
- **THEN** neighboring browsers are not restarted, reassigned, disconnected, or paused by the manual-session manager
- **AND** existing authorized API/MCP operations for those profiles remain available.

#### Scenario: Authorized browser stop during noVNC
- **GIVEN** an administrator or authorized profile user requests an existing stop operation
- **WHEN** the browser run ends according to the existing lifecycle rules
- **THEN** its noVNC access and temporary resources are invalidated and released
- **AND** old manual access cannot follow a subsequent launch of the profile.

### Requirement: MB-14 Verify both deployment architectures

Acceptance SHALL exercise the same manual workflow on ARM64 and x86-64 without GPU. Verification SHALL record memory, CPU, input responsiveness, and process cleanup at 1, 5, and 10 active browsers, before, during, and after one noVNC session. Measurements SHALL identify untested or resource-limited cases rather than assume a RAM budget or equal cross-host performance.

#### Scenario: Target workload is measured
- **GIVEN** approximately 100 saved profiles and controlled, comparable browser workloads on each target host
- **WHEN** the acceptance run measures 1, 5, and 10 active browsers without noVNC, with one session, and after closure
- **THEN** the results record the host architecture, workload, peak memory, CPU, and observed manual-input responsiveness for each case
- **AND** the ordinary/manual/closed temporary VNC-session counts are 0, 1, and 0 without accumulating VNC processes.

#### Scenario: A target host cannot complete a load level
- **GIVEN** a host cannot run a requested load level safely or responsively
- **WHEN** acceptance results are recorded
- **THEN** the case is marked as limited or not passed with its measured cause
- **AND** the report does not turn the default limit of 10 into a claim that the host supports ten browsers or that both hosts have identical graphics fingerprints.
