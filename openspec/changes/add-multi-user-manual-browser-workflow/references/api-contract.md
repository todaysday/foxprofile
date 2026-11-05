# Proposed API and DTO contract

These are proposed new fork endpoints, not existing upstream routes. Mount additions beneath `/api/v1/ext` to reduce future upstream name collisions. Existing routes retain their paths where possible and gain the same principal/policy checks. The implementation may adapt naming to actual router conventions while retaining behavior and updating contract tests.

## Personal identity and administration

| Operation | Caller / result |
| --- | --- |
| `POST /api/v1/ext/auth/login` | Username/password → personal session cookie and redacted current-user DTO. No shared service token returned. |
| `GET /api/v1/ext/auth/me` | Current session → user ID, username, role, permitted presentation data. |
| `POST /api/v1/ext/auth/logout` | Revoke current session, invalidate its connections, clear cookie. State-change protection applies. |
| `GET/POST /api/v1/ext/admin/users` | Admin only: list or create users. Password input is write-only; no hashes in responses. |
| `PATCH /api/v1/ext/admin/users/{user_id}` | Admin only: role/active status; transactional last-admin protection. |
| `POST /api/v1/ext/admin/users/{user_id}/reset-password` | Admin only: set a new password and revoke affected sessions. |
| `PUT /api/v1/ext/admin/users/{user_id}/profile-assignments` | Admin only: replace this user's assignment set atomically after validating target IDs; other users' grants remain unchanged. |
| `GET/PATCH /api/v1/ext/admin/settings` | Admin only: configured capacity and authoritative total occupancy; patch allows valid capacity only. |

No public bootstrap endpoint, self-registration, email-reset flow, third UI role, or per-user MCP role is introduced. Existing machine-token MCP remains service-only. Role/owner/global settings fields in user profile DTOs are rejected, not mass-assigned.

## Existing profiles and batch operations

The existing profile list, CRUD, cookies, proxy/fingerprint controls, screenshots, page and tab routes call the common protected facade. Existing name inputs resolve to server-owned profile IDs. Changing a name cannot change assignments. DTOs expose only fields needed by each existing operation; a general list need not expose proxy passwords, but authorized profile editing must retain that capability.

`POST /api/v1/ext/browser/start-batch` takes `profile_ids: [id, ...]`. The server resolves and authorizes every target, deduplicates, handles existing runs under current semantics, and reserves every required new place in one transaction. Capacity/authorization failure starts none. Successful admission returns allowed per-profile run IDs and status; later spawn errors are reported per target, not misrepresented as atomic startup. Preserve current single-start accepted/starting semantics, including HTTP 202 when only the caller's wait expires.

Bulk stop/delete/other existing batch operations likewise preflight all targets for access before mutations. Runtime failures after authorized execution begins are not a promise of filesystem rollback for all targets. No new batch feature is required where the original operation does not exist, except the shared batch start needed by the existing UI.

## Managed files

- `POST /api/v1/ext/profiles/import` receives an uploaded archive or an opaque private staging handle, plus explicit new-import or overwrite-target intent. No caller-supplied absolute server path is accepted. Authorize overwrite before extraction; new import gets a fresh local ID and creator grant.
- `POST /api/v1/ext/profiles/{profile_id}/export` creates an authorized export using existing portable ZIP behavior, with or without browser data as upstream supports. Return an opaque authenticated download handle or stream it directly.
- `GET /api/v1/ext/downloads/{download_id}` rechecks the principal, original profile and download ownership before streaming. Handles expire and become unusable after access revocation/deletion. No bearer capability to a foreign file is created merely by guessing its ID.
- Cookie import/export follows the same scoped input/output model and preserves upstream stopped/running constraints. Do not add live-profile cookie tooling simply because a browser is already running.
- Legacy path-based transfer endpoints are not available to ordinary users. Preserve configured trusted service compatibility only through explicitly classified paths/roots; personal frontend code never sends the global token to use them.

Do not export users, sessions, assignments, local runtime IDs, service keys, `.env`, extension DB or sibling profile files. Incoming archive metadata cannot grant access, change role, escape staging, or replace global settings. Imported source-local IDs are discarded. Portable serialization and local catalog serialization are intentionally distinct.

## Manual sessions

| Operation | Semantics |
| --- | --- |
| `POST /api/v1/ext/profiles/{profile_id}/manual-sessions` | Personal session with access to a supported running profile → claim one manual lease and attach to its current run. No display/port/path argument. |
| `GET /api/v1/ext/manual-sessions/{session_id}` | Authorized owner session (or administrative cleanup/status authority as documented) → minimal state, profile/run IDs and expiry/connection state. No backend credentials. |
| `DELETE /api/v1/ext/manual-sessions/{session_id}` | Close the exact authorized session idempotently; revoke input/stream, clean only its adapter, preserve browser/display. |
| `WSS /api/v1/ext/manual-sessions/{session_id}/ws` | Authenticate cookie/session and Origin, verify owner and profile authorization, verify active run, then bridge. Session ID alone never grants access. |

Tab closure, network loss, session expiry/timeout, and gateway failure each start one maximum 60-second cleanup deadline. Detection, any reconnect grace, input release, VNC-helper termination, and confirmed helper exit all fit within that end-to-end budget. Reconnect grace is included and does not reset the deadline. A still-live or unverified helper keeps the lease occupied; if cleanup is not confirmed by the deadline, return/report `RUNTIME_RECOVERING`, deny a conflicting manual session, and do not claim cleanup succeeded.

Suggested public manual DTO: `session_id`, `profile_id`, `browser_run_id`, `state`, `websocket_path`, and relevant expiry. Keep `DISPLAY`, Xauthority paths, backend host/ports, process IDs and backend tokens private. A second request cannot retrieve another user's connection authorization just because both users can operate the profile.

Use same-origin cookies by default; a short-lived single-use ticket is an alternative only when the verified transport requires it. Close and revoke on disable/revoke/logout/run end. A new WS connection rechecks authority and run identity. Error text names a busy profile only if the requester already has access to it.

There is no `/complete`, help-request ID, agent callback or Pause/Resume endpoint.

## Error mapping

Reuse the current response envelope if it can express stable codes. The following mapping is proposed for new routes; preserve documented legacy client behavior where practical.

| Code | Typical HTTP | Meaning |
| --- | --- | --- |
| `AUTH_REQUIRED` | 401 | Missing, expired or revoked personal/service authentication. |
| `FORBIDDEN_OR_NOT_FOUND` | 404 | Profile/resource does not exist or is not visible to this caller. |
| `ADMIN_REQUIRED` | 403 | Authenticated ordinary user requests a global administrative action. |
| `VALIDATION_ERROR` | 422 | Invalid DTO/setting; no state change. |
| `PROFILE_CAPACITY_EXCEEDED` | 409 | Atomic admission failed at the configured installation limit. |
| `PROFILE_BUSY` | 409 | Existing same-profile start/tool/operation exclusion applies. |
| `MANUAL_SESSION_BUSY` | 409 | The installation's single manual lease is occupied/provisioning/closing. |
| `MANUAL_UNSUPPORTED_RUNTIME` | 409 | Running engine/mode lacks the required attachable display. |
| `STALE_BROWSER_RUN` | 409 | Known authorized manual request targets an ended/replaced run. |
| `MANUAL_SESSION_CLOSED` | 410 for reconnect | Closed session cannot reconnect. Authorized repeat DELETE succeeds idempotently. |
| `RUNTIME_RECOVERING` | 503 | Liveness reconciliation incomplete; new starts are temporarily disabled. |

MCP surfaces the same domain codes through its current structured tool-result/error conventions. Do not translate capacity exhaustion into an indistinguishable internal-server error. Unauthorized resources are denied before returning state-specific errors that could disclose them.

