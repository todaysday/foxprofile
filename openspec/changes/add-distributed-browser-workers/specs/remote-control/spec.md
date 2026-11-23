## Purpose

Preserve FoxProfile's existing Master REST/MCP/web control and temporary noVNC experience even when the selected browser is running on a VPS Worker.

## ADDED Requirements

### Requirement: DW-C01 One user-facing control entry point
Every UI, REST and MCP browser action SHALL enter through Master and recheck the caller's current authority before forwarding to the exact remote owner. Worker SHALL not expose a separate client endpoint. Existing supported actions SHALL retain their semantics and data formats.

#### Scenario: Agent opens remote profile
- **GIVEN** an authorized agent uses the existing Master MCP tool to navigate a profile
- **WHEN** its browser is on Worker B
- **THEN** Master forwards the action to that same run's PageController and returns the existing response format.

#### Scenario: Unauthorized screenshot
- **WHEN** a principal without profile access requests a remote screenshot
- **THEN** Master denies the operation without fetching or exposing any pixels.

### Requirement: DW-C02 Bound correlated commands
Each remote command SHALL identify immutable profile_id, exact run_id, assigned worker identity and current connection generation, plus a request correlation ID and bounded response. The Worker SHALL only forward to its own current loopback PageController. Raw control-token, localhost port, native process identity or filesystem display path SHALL never appear in user/client output.

#### Scenario: Stop races with click
- **WHEN** a page click arrives after the run entered stopping
- **THEN** it is rejected for the old run rather than sent to a newer profile process.

#### Scenario: Old connection replies late
- **WHEN** a reply from a stale Worker connection arrives after reconnect
- **THEN** Master ignores it and does not complete a new generation's request.

### Requirement: DW-C03 Preserve single protected manual session
Master SHALL retain one installation-wide, principal/run-bound manual lease, including remote runs. Supported remote Camoufox noVNC SHALL attach to the same existing Worker browser and Xvfb through a private outbound RFB relay. It SHALL not launch another browser or require a public VNC listener.

#### Scenario: Concurrent remote manual requests
- **WHEN** users request noVNC for two different remote profiles at the same time
- **THEN** at most one manual session is admitted and the existing authorization/visibility policy governs the loser.

#### Scenario: Viewer to noVNC to Viewer
- **WHEN** a user enters and exits remote noVNC
- **THEN** the run_id, browser, tabs, fingerprint, display and global occupied browser count remain unchanged.

### Requirement: DW-C04 Revocation and temporary cleanup
Manual and ordinary remote control SHALL stop carrying input/images after session expiry, profile access revocation, run termination or stale worker transport. Back/disconnect SHALL release held keys/buttons, terminate only the manual helper and obey the existing 60-second end-to-end cleanup contract. Unconfirmed helper exit SHALL keep manual lease occupied.

#### Scenario: Rights are revoked during remote manual session
- **WHEN** the user's profile grant is removed
- **THEN** further input/pixels stop, the private relay is closed and helper cleanup begins; browser remains available to other authorized users.

#### Scenario: Manual transport dies mid-keypress
- **WHEN** remote noVNC disconnects
- **THEN** held input is released and the next ordinary PageController action still works after verified helper cleanup.

### Requirement: DW-C05 Preserve engine limitations and UI behavior
A remote Worker SHALL not silently replace the user's engine or change screen/window geometry. Existing Camoufox manual support and explicit unsupported Chrome modes remain unchanged until separately implemented. Worker inventory details require administrator access; ordinary profile users see only permitted run status and assigned location without foreign profile names.

#### Scenario: Chrome has no supported noVNC display
- **WHEN** an authorized user asks for manual access
- **THEN** Master returns the existing unsupported-runtime outcome and does not spawn a different browser.
