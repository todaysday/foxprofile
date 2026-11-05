# Upstream update and compatibility runbook

## Ownership

The implementation agent prepares and validates this workflow offline in slice 8. A separate deployment agent executes operational update/promotion and live acceptance in final slice 9. No automatic update of the production checkout or server from upstream HEAD is part of this change.

## Repository organization

- Keep upstream commit ancestry intact and use a named upstream remote pointing to `https://github.com/chienbm98/foxprofile.git`.
- Keep extension behavior in `foxprofile_ext/`, extension-specific assets/config/tests in their own locations, and minimal upstream integration edits in small commits with clear purposes.
- Preserve existing upstream interfaces and profile formats except for deliberate documented authorization, stable-ID and atomic-batch changes.
- Avoid unrelated dependency upgrades, repository-wide formatting, file moves, copied upstream classes and edits inside vendored third-party code.
- Maintain an integration-hook register: upstream file/symbol, hook purpose, expected contract, covering test, and baseline commit. Update it when a real seam changes; do not invent a generalized plugin platform.

## Required compatibility record

The implementation supplies a machine-readable or simple structured file containing:

| Field | Required content |
| --- | --- |
| Upstream baseline | Exact upstream commit and release/version where available. |
| Fork candidate | Exact immutable commit being delivered. |
| Schema | Extension schema/migration version and catalog ID format. |
| Runtime | Python, Camoufox library/browser, Playwright, noVNC, VNC adapter and confinement mechanism versions relevant to the tested environment. |
| Deployment | Supported process topology, engine scope, OS/architecture evidence. |
| Hooks | Finite changed-file/symbol map and tests. |
| Offline evidence | Test commands/results and artifact hashes. |
| Live evidence | Separate final-agent report references; initially NOT RUN. |

Do not use this record to block all version changes forever. It records a known working combination, while adapter tests establish whether the next candidate is compatible.

## Offline candidate preparation

1. Start from a clean disposable branch/worktree for the candidate. Record current fork/upstream commits and select an explicit upstream commit/tag to evaluate. Do not merge in the production checkout.
2. Merge or rebase according to the repository's existing history policy; prefer preserving shared branch history. Never force-push a shared branch just to update this fork. Resolve conflicts locally in the finite seams.
3. Inspect the upstream diff for new/changed API routes, direct UI/service actions, profile tools, browser spawns, process events, serialization, dependency and deployment defaults. A clean textual merge is not compatibility evidence.
4. Update adapters only where their contracts changed. Keep shared policy in the extension; do not patch every new route with divergent permission logic.
5. Re-run route/launch surface classification. New public routes must be explicitly reviewed as non-sensitive; every protected data/launch path requires principal/admission context. Unknown paths fail the compatibility gate.
6. Run upstream tests and focused extension tests: auth/revocation, catalog/ID/export round-trips and interrupted operations, shared capacity/batch/race/recovery, cookie helper, runner control/display lifecycle, manual lease/WS cleanup, UI mount/return and confinement fixtures.
7. Rehearse schema migration and restore against copied disposable catalog/browser fixtures, including older valid capacity values and residual failed-delete directories.
8. Produce a locked candidate and evidence report. Classify failures as upstream pre-existing, extension regression or unsupported contract. A required test failure blocks readiness for deployment; do not weaken assertions to get a green result.
9. Deliver candidate and the updated hook/compatibility record to the final deployment agent. The live acceptance plan and production state remain unchanged until that agent executes its slice.

## Concrete contract suite

| Contract | Regression it must detect |
| --- | --- |
| Public/protected route inventory | New export, screenshot or control route exposed without classification/authentication. |
| Principal-required service facade | Flet/background/direct service path silently becoming trusted when no principal is supplied. |
| MCP service boundary | Personal session accidentally forwarded through the global machine token. |
| Local versus portable profile serialization | ID dropped on load/rename, foreign ID imported, extension authority exported. |
| Catalog-operation recovery | Silent save error, residual old cookie directory or half-renamed profile handed to a new user. |
| Launch admission | New helper/background path spawns without capacity; worker-local counter replaces shared reservations. |
| Process handshake | CONTROL/metadata ordering changes, secrets logged, stop event treated as confirmed exit. |
| Runtime recovery | PID/display reuse attaches to an unrelated process or old run cleanup frees a new reservation. |
| Manual UI/gateway | Separate browser created, screen resized, ordinary input duplicated, agent resume added, stale WS accepted. |
| Confinement | Native browser controls can read sibling/service files or inherited service environment. |

Tests should use representative protected-operation categories and generated inventories where possible. Do not duplicate every route's implementation in a brittle test or assert incidental line numbers. A targeted test that demonstrates a bypass remains required even if an inventory also lists that route.

## Promotion and rollback — final agent only

The final agent verifies hashes and the real environment, creates consistent backups, rehearses on an isolated copy, deploys the locked candidate, and executes the final slice's live matrix. Preserve WSS and private X socket namespace behavior; do not disable service hardening merely to make VNC connect.

Rollback restores a compatible code/config/data set and resumes the tested protected deployment. A raw upstream checkout is not a drop-in rollback for an extension-protected multi-user installation. Never run an older serializer that drops IDs over the only production catalog. If a schema is not backward compatible, restore the matching backup rather than promising a downgrade that has not been tested.

Maximum practical update resilience means small explicit seams plus tests and controlled promotion. It is not a guarantee that future upstream changes will require zero adaptation.

