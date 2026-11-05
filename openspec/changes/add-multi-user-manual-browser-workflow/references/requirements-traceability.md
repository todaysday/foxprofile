# Requirements traceability

**Purpose:** show where every supplied source concern and each additional user direction is represented in the OpenSpec package. These mappings are specification coverage, **not evidence of implementation or passed tests**. Mandatory implementation acceptance remains **NOT RUN** in [acceptance-plan.md](acceptance-plan.md); only the conditional real-upstream-update rehearsal is N/A for initial release without a selected candidate.

## Sources and capability keys

| Key | Authoritative input |
| --- | --- |
| Access | [FoxProfile_User_Access_Spec.md](inputs/FoxProfile_User_Access_Spec.md), v0.2. |
| noVNC | [FoxProfile_noVNC_Manual_Handoff.md](inputs/FoxProfile_noVNC_Manual_Handoff.md), v1.1. |
| Stealth | [FoxProfile_Stealth_Configuration.md](inputs/FoxProfile_Stealth_Configuration.md). |
| Conversation | User directions added during package authoring: upstream-update resilience; `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY`; live deployment and acceptance as a separate final slice executed by a different agent. |

| Capability | Requirement prefix |
| --- | --- |
| [user-authentication](../specs/user-authentication/spec.md) | UA |
| [profile-authorization](../specs/profile-authorization/spec.md) | PA |
| [global-profile-capacity](../specs/global-profile-capacity/spec.md) | GC |
| [virtual-browser-runtime](../specs/virtual-browser-runtime/spec.md) | VR |
| [manual-browser-access](../specs/manual-browser-access/spec.md) | MB |
| [persistent-stealth](../specs/persistent-stealth/spec.md) | ST |
| [upstream-compatibility](../specs/upstream-compatibility/spec.md) | UC |

## 1. Access specification

| Source section | Requirement coverage | Acceptance coverage |
| --- | --- | --- |
| §1: extend the existing panel, two roles, full assigned-profile control, shared default 10 | UA-01; PA-03, PA-04; GC-01, GC-03; UC-01 | A01–A30; U01 |
| §2.1: exactly administrator and user | UA-01, UA-04; PA-01, PA-03 | A10, A11, A16, A20 |
| §2.2: complete operation matrix, no hidden restrictions, agent-started Viewer | PA-01, PA-03, PA-13; UA-07; GC-02, GC-03; VR-06; MB-02 | A03–A10, A16, A19, A24 |
| §2.3: new/clone/import ownership; no automatic existing-profile access; retain disabled-user profiles; only existing operations | PA-01, PA-02, PA-03, PA-09; UA-04 | A14, A15, A17 |
| §2.4: durable setting, lifecycle accounting, all paths, atomic admission/batch, errors, cleanup/recovery, decreases | GC-01, GC-02, GC-03, GC-04, GC-05, GC-06, GC-07, GC-08, GC-09, GC-10, GC-11 | A21–A30; X01–X03 |
| §3.1: full ordinary panel, server-filtered lists/search/counts/groups/logs, no secrets or foreign profiles | PA-03, PA-06; UA-07; GC-11 | A02, A05, A06, A10, A18, A24 |
| §3.2: minimal user administration, assignments, existing global settings, capacity UI/occupancy | UA-04, UA-07; PA-11; GC-01, GC-10, GC-12 | A16, A26–A28 |
| §3.3: server-derived principal, role only controls display, filtered server responses | UA-01, UA-03; PA-04, PA-06 | A02, A11, A18 |
| §4: users/sessions/assignments, stable profile ID, atomic create+assign, server-controlled privileged fields | UA-01, UA-03; PA-01, PA-02, PA-11, PA-12; GC-01; UC-06 | A11, A15, A17; X09; U05 |
| §5.1: centralized authorization before protected reads or mutations | UA-01, UA-03; PA-04; UC-04, UC-07 | A01, A08–A12; U02, U03 |
| §5.2: every API/Viewer/file/control surface; alternate IDs; whole-batch denial | PA-03, PA-04, PA-05, PA-06, PA-07, PA-09, PA-10; GC-03, GC-05; MB-05 | A08–A12, A18; X01, X05 |
| §5.3: revoke ongoing commands/images without killing the browser | UA-03, UA-04; PA-08; MB-05 | A13, A14; N12 |
| §6.1: personal credentials, strong hashes, revocable sessions, HTTPS/cookies/CSRF, administrator-created users | UA-01, UA-02, UA-03, UA-04 | A01, A14, A16, A17; X09 |
| §6.2: trusted service credential separate from user sessions; no leaked shared token; no per-user MCP expansion | UA-06, UA-07; GC-03 | A18, A19, A22 |
| §7: own proxy credentials, no foreign proxy-library leaks, safe system/file boundaries and imports/exports | PA-09, PA-10, PA-12, PA-14; MB-07, VR-05 | A05, A06, A08, A18; X08, X09 |
| §8: preserve browser data/identity, same session, same-profile locks; no new human/agent coordinator | UA-09; PA-11, PA-12, PA-13; VR-04, VR-06; ST-02, ST-03; MB-02, MB-09, MB-12 | A04, A13, A14, A17; N02, N05; X01, X07 |
| §9: safe bootstrap, no ordinary-user access to existing/unassigned profiles, last admin, safe migration/default and retained limit | UA-05, UA-06, UA-08, UA-09; PA-01, PA-12; GC-01, GC-09, GC-10; UC-06, UC-08 | A17, A20, A21, A27, A30; U05, U06; final slice 09 |
| §10: 30 explicit acceptance checks | All access/capacity requirements, with overlapping runtime/manual checks | Exact one-to-one rows A01–A30 in acceptance-plan.md |
| §11: inspect actual paths; reuse appropriate existing mechanisms; implement incrementally | UC-01, UC-02, UC-03, UC-04, UC-07 | O01–O07; U01–U04 |
| §12: no organizations/SSO/per-user quotas/queue/manager rewrite/employee-audit expansion | Scope constraints in proposal/design; GC-05; UC-01, UC-02; MB-12 | U01; scope review |
| §13: unified completion criterion | Seven capability specs together | Final completion gate; final deployment agent report |


## 2. noVNC specification

| Source section | Requirement coverage | Acceptance coverage |
| --- | --- | --- |
| §1: existing hosts/no GPU, ~100 saved/10 active, existing manager, one manual session, no distributed scheduler | GC-01, GC-02; MB-01, MB-02, MB-03, MB-13, MB-14; VR-07, VR-08 | A21–A24; N01, N08, N13 |
| §2: preserved Playwright mouse and Camoufox humanize; raw VNC input is distinct | ST-04; MB-08 | N03, N11; S03 |
| §3: attach existing display/browser; existing virtual path first; no duplicate Xvfb or true-headless switch | VR-01, VR-03, VR-04; MB-02, MB-07; ST-01 | N02, N04; X04, X06 |
| §4: profile-owned identity, per-profile exclusivity, unique run/host binding, stable geometry, no fictitious GPU | VR-02, VR-05, VR-06; MB-04, MB-08; ST-02, ST-03, ST-05, ST-06 | N02, N09, N10; S02, S03; X01 |
| §5: embedded button/Back, same screen, suspend duplicate handlers and hidden refresh, restore ordinary controls | MB-01, MB-08, MB-09, MB-10, MB-12 | N01, N03–N05, N07, N10; X07 |
| §6: agent remains independent; no completion adapter/events/help IDs/pause/resume | MB-09, MB-12 | N05, N06; X07 |
| §7: server-owned run descriptor, temporary manager, authenticated gateway, run-bound APIs and close order | VR-02, VR-03, VR-04, VR-05; MB-04, MB-05, MB-07, MB-10 | N02, N04, N07, N09, N12; X04, X05 |
| §8: duplicates/busy, disconnect/reconnect, reused run/display, idempotent close, released input, no agent task change | MB-03, MB-04, MB-10, MB-11, MB-12, MB-13 | N06–N09, N11; X04 |
| §9: permissions, temporary access, HTTPS/WSS/local VNC, Origin/CSRF, safe process/Xauthority boundary | PA-14; MB-04, MB-05, MB-06, MB-07; VR-03, VR-05 | N12; X05, X08 |
| §10: on-demand VNC only; measured resources at 1/5/10 on both architectures; no invented RAM claims | GC-02; MB-02, MB-03, MB-14; VR-08 | N13; resource matrix in acceptance-plan.md |
| §11: staged implementation; no new agent adapter/queue/custom REST drag/desktop environment/fingerprint rewrite | MB-07, MB-08, MB-12; ST-02, ST-05; UC-01, UC-02 | U01; final slice 09; scope review |
| §12: 13 explicit acceptance checks | MB-01–MB-14 plus runtime/stealth intersections | Exact one-to-one rows N01–N13 in acceptance-plan.md |
| §13: evidence status; user-verified snippets not universal upstream claims | UC-03, UC-07; ST-04 | Baseline/source audit; U02 |

## 3. Stealth configuration

| Source section | Requirement coverage | Acceptance coverage |
| --- | --- | --- |
| §1: `FOXPROFILE_HEADLESS=virtual` for panel and agent, next-launch effect | ST-01; VR-01 | N02; S03; configuration review |
| §2: saved fingerprint/data/proxy, saved or GeoIP locale/timezone, humanize, Linux-new-only, native WebGL/Canvas | ST-02, ST-03, ST-04, ST-05; VR-06 | A05, A17; S01, S02; configuration review |
| §3: permitted 1366×768 baseline, persistent geometry, local scaling/no resize, existing display, Back independence | ST-06; MB-02, MB-08, MB-10, MB-12 | N02, N04, N05, N10; S03 |
| §4: representative profile on each host; IP/WebRTC, restart persistence, same-run noVNC; no speculative patches | ST-07; MB-14; VR-08 | Exact one-to-one rows S01–S03; N13 |

## 4. Additional user directions

| Direction | Requirement or plan coverage | Evidence expected |
| --- | --- | --- |
| Make the fork maximally robust to upstream updates | UC-01, UC-03, UC-04, UC-05, UC-06, UC-07, UC-08; thin integration hooks, finite adapters, fail-closed activation, explicit update candidates | U01–U06; compatibility manifest; no claim of arbitrary future compatibility |
| Apply `Pragmatic DDD (DAO/DTO) → SRP → KISS/YAGNI → DRY` | UC-02; implementation config/design/tasks and code-review gate | O01, O03; U01; domain logic separate from storage/transport without ceremonial architecture |
| Deployment and live acceptance are a separate final slice owned by a separate agent | UC-09; [Slice 09](../slices/09-live-deployment-and-acceptance.md); acceptance-plan §§1–2/9; main tasks section 9 | Named ownership and locked-commit handoff; implementation agent's offline evidence; separate deployment agent's real-host report |

## 5. Reconciled boundaries and explicit choices

1. Access §12 excludes noVNC from that individual document. The combined package includes noVNC because the companion noVNC document explicitly requests it; it does not reinterpret the exclusion as a ban on the complete package.
2. noVNC's busy-profile message is filtered through Access privacy requirements: show an occupied profile only to an authorized viewer; otherwise return a generic busy response (MB-03).
3. Both sources explicitly exclude new human/agent orchestration. Local UI input suspension is not an agent pause or server-wide action interlock (MB-09, MB-12).
4. The installed virtual-display path is reused and extended through a thin adapter/hook. Camoufox is the first supported noVNC runtime; an unsupported Chrome run is preserved with a clear error (VR-01, VR-07, MB-01).
5. The 1366×768 new-profile baseline is an allowed implementation choice, conditional on fitting the full browser. Existing coherent geometry and existing saved OS/fingerprint are preserved (ST-05, ST-06).
6. First-version assignments use explicit full-control grants through `UserProfileAccess`; simple administration does not require a complex shared-ownership UI. Every grant has the same complete rights (PA-01, PA-11), and stable identity is not replaced by display-name matching.
7. Native browser input can reach OS file dialogs and file URLs. The Access §7/noVNC §9 boundary therefore needs the runtime's real per-profile confinement and sanitized child environment (PA-14), tested by X08; filtering HTTP fields alone cannot demonstrate isolation.
8. "Ready for deployment" and "live acceptance passed" are distinct outcomes. All real-server work is reserved for the separate final deployment agent; this package records no executed implementation acceptance.
