# Change: Multi-user access and manual browser control for the FoxProfile fork

## Why

The existing FoxProfile installation must serve administrators, ordinary users, and a trusted automation agent without exposing another user's browser profiles. It also needs a shared active-profile limit and an embedded noVNC view of the exact browser already running, for occasional manual gestures.

The implementation must preserve working Pinterest browser identities and remain practical to update from upstream. The audited upstream at `530b993ff3a7d211aaa0904fba0817b77c75d4c9` already supplies persistent profiles, REST/MCP, full-profile transfer, and Camoufox virtual displays. It does not supply the requested per-user access model, durable global capacity policy, or embedded noVNC workflow. See [source audit](references/upstream-audit.md).

## What Changes

- Add only `admin` and `user` roles, personal login/logout/revocable sessions, profile assignments, and a minimal administrator user-management screen. Assignment grants all existing profile operations, including settings, proxy, fingerprint, cookies, full-profile transfer, and deletion.
- Protect and filter existing panel, REST, direct-service, Viewer, screenshot, file-transfer, and WebSocket surfaces; keep trusted service-only MCP separate from personal panel credentials.
- Add durable installation setting `max_concurrent_profiles`, default 10, and shared atomic reservations for all launch paths, including batch requests and browser-backed cookie helpers. Running, starting, reserved and stopping sessions count; reconnecting to a running profile does not.
- Expose stable local profile IDs and unique run/display bindings by extending existing launch/display code. Retain `FOXPROFILE_HEADLESS=virtual`, `humanize=True`, saved identity and the working Camoufox Xvfb implementation.
- Add **«Открыть в noVNC»** above the existing profile Viewer, embedded in the same screen, and **«Вернуться назад»** to close manual resources and restore ordinary control. Allow one temporary manual session per installation; no new browser and no remote geometry change.
- Keep noVNC independent of the agent. No completion messages, help-request IDs, agent adapter, CAPTCHA-result decision, global pause system, or automatic automation resumption.
- Isolate extension modules and assets, minimize explicit upstream edits, add adapter compatibility checks and an upstream-update runbook. Apply **Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY**.
- Separate implementation/offline verification (slices 1–8) from deployment and real-host acceptance by a different agent (final slice 9).

## Capabilities

### New Capabilities

- `user-authentication`: personal sessions, two roles, trusted machine access and safe migration/bootstrap.
- `profile-authorization`: full control of assigned profiles, server-side filtering and resource/file isolation.
- `global-profile-capacity`: durable shared capacity, atomic single/batch admission, termination and recovery.
- `virtual-browser-runtime`: same-run display discovery, process identity and existing lifecycle integration.
- `manual-browser-access`: embedded noVNC, authorization, one-session lease and cleanup without agent integration.
- `persistent-stealth`: saved identity, configured virtual mode, native Camoufox behavior and geometry stability.
- `upstream-compatibility`: isolated extension design, minimal hooks, contract checks and controlled updates.

### Modified Capabilities

None in an existing OpenSpec baseline: the audited upstream has no OpenSpec tree. All seven deltas are ADDED formal specifications even where implementation extends an existing feature. They do not assert those features are already implemented.

## Impact

**Users.** Personal login replaces sharing the machine token for ordinary panel access. Existing profiles initially remain administrator/service accessible; users see them after assignment. Full permitted controls remain available in the current panel.

**APIs.** Existing profile and browser operations keep their compatibility where practical, but authorization becomes mandatory for protected operations. Add personal-session, administration, batch-admission and manual-session routes. Arbitrary server filesystem paths are no longer accepted from ordinary users; managed upload/download preserves the existing import/export capability. Existing service MCP retains its separately authenticated machine context.

**Runtime.** Reuse Camoufox's current VirtualDisplay and cleanup rather than add a competing browser manager. All engines count toward installation capacity; initial noVNC/stealth support targets Camoufox on Linux ARM64/x86-64 without GPU. Existing Chrome operations remain available, with an explicit unsupported manual-runtime state.

**Persistence.** Add an immutable local profile ID through the smallest model/serializer change. Keep upstream JSON catalog and browser data in place; store users, sessions, ACLs, settings and run/manual leases in an additive local SQLite sidecar through DAOs. Use bounded recovery for catalog/ACL/filesystem operations.

**Dependencies and deployment.** Add a pinned noVNC client and replaceable local VNC adapter, initially x11vnc after build/support review. Reuse the Python runtime and SQLite standard library; no separate database server, Redis, desktop environment, cross-host scheduler or message bus. Deployment assets must preserve the existing service namespace and enable WSS forwarding.

**Integration footprint.** Small hooks in container/auth/API composition, profile serialization and lifecycle, browser process events, bulk actions, current Viewer, and deployment config; new behavior lives under a separate extension package. The precise hook register and contract evidence are required implementation outputs.

## Non-Goals

Organizations, teams, billing, per-user quotas, permission builders, SSO, self-registration, email recovery, a second control panel, new profile operations absent upstream, an audit product, queues/priorities/preemption, distributed scheduling, custom mouse trajectories, modified browser engines, automatic CAPTCHA solving, agent orchestration, or guarantees against platform checks.

This package specifies the fork; it does not contain implementation code, a deployed fork, or live acceptance results.

## Execution Boundary

`IMPLEMENTATION_AGENT` completes tasks 1–8, supplies a tested commit and a complete handoff, and leaves task group 9 unchecked. `DEPLOYMENT_AGENT`, a separate agent designated for the last slice, performs all actual deployment, operational migration, and live acceptance using [slice 9](slices/09-live-deployment-and-acceptance.md). Archiving the change requires the final evidence; completing the offline slice alone is not production acceptance.

