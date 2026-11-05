## Purpose

Preserve the existing Camoufox profile identity and proxy behavior across browser launches and temporary manual access on the user's ARM64 and x86-64 servers, using the supplied configuration without adding speculative stealth mechanisms.

## ADDED Requirements

### Requirement: ST-01 Virtual display launch policy

The deployment SHALL configure `FOXPROFILE_HEADLESS=virtual` for supported Camoufox launches from both the panel and agent. Launches SHALL follow the virtual-browser-runtime capability and SHALL NOT use true `headless=True` or switch rendering mode to open noVNC. A configuration change SHALL take effect on the next browser launch without restarting an active run for manual access.

#### Scenario: Panel and agent use the same supported policy
- **GIVEN** the deployed service is configured with `FOXPROFILE_HEADLESS=virtual`
- **WHEN** either the panel or the trusted agent launches a Camoufox profile
- **THEN** the browser starts on the attachable virtual display managed by the selected runtime strategy
- **AND** manual access later attaches without a mode switch or another display layer.

#### Scenario: An incompatible running mode is encountered
- **GIVEN** an old browser run has no attachable display because it was started in true headless mode
- **WHEN** manual access is requested
- **THEN** the system reports that the run cannot provide noVNC access
- **AND** it does not silently restart the browser; the corrected launch policy applies to its next explicit launch.

### Requirement: ST-02 Durable profile identity and data

Fingerprint, persistent browser data, cookies, and profile configuration SHALL belong to the saved profile, not a runtime slot. Launches SHALL reuse the saved fingerprint and the profile's own persistent directory. Only an explicit authorized profile operation may intentionally replace them. noVNC open/close SHALL neither regenerate identity nor copy data into a temporary human profile.

#### Scenario: Restart preserves account and fingerprint
- **GIVEN** a profile has a saved fingerprint and an existing login in its persistent directory
- **WHEN** it is stopped and explicitly launched again, including in a different available slot
- **THEN** the same fingerprint and persistent login data are loaded
- **AND** another profile's browser directory is not reused.

#### Scenario: First launch followed by manual access
- **GIVEN** a new profile needs its initial fingerprint according to the existing FoxProfile mechanism
- **WHEN** the first launch creates and saves that fingerprint and the user later opens and closes noVNC
- **THEN** the fingerprint remains the saved one
- **AND** neither noVNC nor another ordinary launch triggers an automatic replacement.

### Requirement: ST-03 Stable proxy and locale configuration

The system SHALL keep the proxy bound to the profile and reuse the existing supported proxy-based GeoIP behavior or saved timezone/locale settings. Slot assignment and noVNC lifecycle SHALL NOT rotate proxies, randomly change language/timezone, or alter credentials. Explicit authorized profile changes SHALL remain available under existing stopped-browser requirements.

#### Scenario: Temporary manual access preserves egress configuration
- **GIVEN** a running profile has a configured proxy and effective locale/timezone
- **WHEN** the user opens noVNC, resizes the client view, and returns
- **THEN** the profile retains the same proxy binding, credentials, locale, and timezone configuration.

#### Scenario: Authorized proxy edit remains possible
- **GIVEN** a user can edit the assigned profile and satisfies the existing browser-state rules for a proxy change
- **WHEN** the user explicitly saves a different supported proxy or pinned locale/timezone
- **THEN** the existing profile-edit workflow remains available
- **AND** subsequent launches use the saved configuration rather than silently restoring a slot-level default.

### Requirement: ST-04 Retain existing Camoufox input behavior

The Camoufox launch configuration SHALL retain `humanize=True`, and ordinary coordinate-click control SHALL retain the existing Playwright mouse path. The change SHALL NOT add a separate mouse-trajectory generator or replace ordinary clicks with JavaScript `element.click()`. It SHALL NOT claim that Camoufox humanization processes raw VNC gestures or makes all human and agent behavior identical.

#### Scenario: Ordinary coordinate click after manual access
- **GIVEN** the pinned source revision has been checked for its Camoufox launch and coordinate-click paths
- **WHEN** the panel or agent performs an ordinary coordinate click after noVNC closes
- **THEN** the call uses the preserved Playwright mouse mechanism with the retained Camoufox launch setting
- **AND** there is no new JavaScript click substitution or custom movement layer.

#### Scenario: Human directly controls the VNC pointer
- **GIVEN** the user moves and drags through noVNC
- **WHEN** the system forwards those VNC input events
- **THEN** the manual path remains direct human input to the existing display
- **AND** implementation documentation does not describe that path as passing through Playwright or being additionally humanized by `humanize=True`.

### Requirement: ST-05 Minimal graphics configuration

New Camoufox profiles SHALL default to Linux; existing profiles SHALL NOT be mass-converted. The change SHALL keep WebGL enabled and preserve native Camoufox Canvas behavior without forcing `canvas_noise=True/False`, inventing a GPU identity, or adding JavaScript overrides for UA, Canvas, fonts, or `navigator.webdriver`. Passing verification SHALL NOT trigger extra stealth patches.

#### Scenario: Create a profile on a host without GPU
- **GIVEN** a new profile is created on either target architecture without a GPU
- **WHEN** its defaults are prepared
- **THEN** Linux is selected and WebGL remains enabled through the supported native configuration
- **AND** no fabricated graphics-card identity, forced Canvas flag, or JavaScript fingerprint override is added by this change.

#### Scenario: Existing profile survives migration
- **GIVEN** an existing profile has a previously saved supported OS and fingerprint
- **WHEN** this change is installed or its first post-upgrade launch occurs
- **THEN** the profile is not automatically converted to Linux or assigned a replacement fingerprint
- **AND** existing authorized explicit fingerprint-edit operations remain available.

### Requirement: ST-06 Stable and coherent screen geometry

New profiles SHALL use the selected 1366×768 baseline when the full browser window fits; existing saved coherent geometry SHALL be preserved. Display size, browser-window size, and page-visible screen values SHALL be verified as related but distinct values. Ordinary relaunches and manual access SHALL preserve saved dimensions instead of adapting them to the user's phone or desktop.

#### Scenario: Verify new-profile geometry
- **GIVEN** a new profile uses the 1366×768 baseline
- **WHEN** its display and browser are launched and inspected
- **THEN** the complete intended browser window fits and the visible screen configuration is coherent with the saved profile
- **AND** verification does not assume that display size, viewport size, and page-visible screen size are identical measurements.

#### Scenario: Existing larger geometry on a small client
- **GIVEN** an existing profile has saved coherent dimensions different from the new-profile baseline
- **WHEN** it is viewed through noVNC on a small screen and subsequently relaunched
- **THEN** its saved remote dimensions remain unchanged
- **AND** only client-side scaling adapts the presentation.

### Requirement: ST-07 Verify egress and persistence per host

Before acceptance, one representative profile on each target architecture SHALL be checked for proxy egress, public-server-IP non-disclosure through WebRTC, persistent login/fingerprint across restart, and same-run noVNC transitions. Failures SHALL be recorded and corrected within scope. Passing checks SHALL NOT be presented as a guarantee against CAPTCHA or detection.

#### Scenario: Egress and session checks pass on each host
- **GIVEN** a representative profile has a known proxy and a controlled login on each target host
- **WHEN** IP/WebRTC checks, a browser restart, and a noVNC open/return cycle are performed
- **THEN** egress follows the configured proxy, the public server IP is not exposed through the tested WebRTC path, login and fingerprint survive restart, and noVNC preserves the run and geometry
- **AND** ordinary clicks work after the manual session closes.

#### Scenario: An observed leakage or persistence regression
- **GIVEN** a verification exposes the public server IP or loses the saved identity/session
- **WHEN** results are assessed
- **THEN** the failing case is reported as unaccepted and investigated in the actual proxy, runtime, or persistence path
- **AND** the implementation does not conceal the failure with a claim of universal stealth or introduce unrelated speculative patches.
