# Distributed browser workers — implementation evidence

Date: 2026-11-23. Branch: `codex/openspec-multi-user-manual-browser-workflow`.
Implementation starts from `cea6a094659edc7e30b1707dd770a2e47c2463cf`.
Runtime-source checkpoint: `1b58eb746e20bae2d11f777891fba28fe05c9a76`. Later evidence metadata does not change executable source.

GitHub API publication assigned the checkpoint above while preserving the complete
tested implementation tree, `dcf8f8ff7694eae2bfbec41a8676a08c567a9edd`.
Only commit identity and the later metadata references changed; the executable
source and tests remain byte-for-byte identical to the verified local checkpoint.

This record describes implementation and offline verification. It does not close
two-host deployment acceptance or the previous manual-browser change's slice 09.
The change remains unarchived.

## Implemented boundaries

The existing protected application, profile catalog, capacity ledger,
`RuntimeCoordinator`, confined `RuntimeSupervisor`, PageController and
`ManualSessionService` remain the authorities. Distributed mode is explicitly
enabled by the Master configuration; ordinary clients keep the Master REST/MCP
and Viewer entry points.

- Schema v6 retains one active run per immutable profile ID across `reserved`,
  `transferring`, `starting`, `running`, `stopping`, `syncing` and `uncertain`.
  Worker placement metadata is separate from local process identity. Old remote
  PIDs are never used as local death evidence.
- Admission reserves complete batches in one SQLite transaction, enforcing the
  installation-wide limit and projected memory. It prefers the Master and then
  the eligible Worker with greatest safe headroom. Fresh observed running
  allocations are not counted twice; newer and uncertain allocations remain
  charged. Stopped-profile helpers reserve through the same ledger and stay on
  the Master.
- Worker credentials are hashed at rest on the Master. The outbound WSS channel
  binds Worker, boot and connection generation; commands/results also bind
  profile and run. Heartbeats are 10 seconds, freshness/loss is 30 seconds and
  control frames are bounded at 16 MiB. Non-heartbeat traffic cannot renew the
  heartbeat deadline.
- The canonical profile remains on the Master. Transfer capabilities bind the
  exact owner, generation, operation, revision and archive digest. Private
  immutable ZIP snapshots carry browser data and fingerprint identity, excluding
  Master metadata. Launch settings are refreshed and frozen under the same
  catalog lock as snapshot capture, so edits made before dispatch cannot leave
  the Worker with stale proxy/locale/timezone settings. Size/member/path/link
  checks precede activation. Full-data
  return uses a recoverable filesystem intent and an idempotent durable receipt.
- A confirmed clean stop enters `syncing`; failed upload retains the slot and
  **Stop** retries saving. Unconfirmed remote liveness enters `uncertain` and
  retains both profile exclusivity and its reservation. An authenticated exact
  stop proof or explicit administrator attestation of independently verified
  host fencing is required before quarantine and release. A timeout alone never
  releases a run. A lost start acknowledgment can be resolved by explicit Stop
  only on the original still-current connection. Late clean proof on that
  connection can still save the run; a previous connection generation cannot
  resume control or overwrite the canonical copy. The durable unsaved outcome
  appears in authorized profile rows after quarantine without consuming capacity.
  Cancelled or proven pre-spawn failures do not erase it; a later successful
  saved session clears it.
- Remote noVNC uses the existing installation-wide personal manual lease. The
  Worker prepares a gated helper; the Master records its identity before
  activation. A private outbound binary relay carries RFB. **Back** closes the
  helper and relay without stopping the browser or reserving another slot.
  Confirmed exact-run helper cleanup is retained across Master restart and
  connection-generation changes; unknown cleanup keeps the lease blocked.
- Once Master durably closes a run, it acknowledges the exact stop proof over
  the authenticated same-Worker/same-boot channel. This terminal operation can
  use a newer transport generation solely to remove isolated run data. It
  cannot revive the run or authorize an upload. Worker verifies browser and
  helper death, retains recent durable replay evidence and removes acknowledged
  proofs from its repeated heartbeat inventory. Unknown cleanup stays retained.
- Administrator inventory and drain controls are in the existing administration
  dialog. Ordinary profile rows expose only authorized state and local/remote
  location, without Worker credentials, private addresses, PIDs or display data.

## Verification scope

Verification uses disposable files/databases and synthetic profile data. The
Master integration fixture changes cookies, local storage and tab state in a
private Worker copy, transfers real ZIP bytes through FastAPI HTTPS routes and
commits them to the canonical directory. This proves the transfer and lifecycle
contracts in the fixture; it is not a real browser/login or two-host result.
A second integration uses the actual WorkerAgent, Master coordinator, locally
trusted TLS/WSS, HTTPS snapshot/return routes and terminal acknowledgment. TLS
verification is enabled; only native browser execution is replaced by a runtime
fixture. The watchdog test launches real isolated stand-in process groups and
verifies that a separate interpreter terminates them while the agent-side test
thread makes no event-loop progress.

| Acceptance cases | Offline coverage | Live boundary still required |
| --- | --- | --- |
| DW-01–03 | Worker registry, authenticated WSS and manifest validation tests | Installed Ubuntu runtime/native renderer and both architectures |
| DW-04–08 | Atomic global/profile/RAM admission, deterministic placement and races | Measured browser RSS and actual process-tree counts under load |
| DW-09 | Existing stopped-profile policy and shared capacity regressions | Actual cookie-helper/browser overlap on deployed hosts |
| DW-10–14, DW-25 | Complete archives, preserved identity and Master metadata, streamed interruption, generation/revision checks, receipt retry and journal recovery | Real browser login/storage/tabs persistence across both hosts |
| DW-15–16 | Existing protected adapters, exact-run channel correlation, authorization revocation, bounded frames and private relay denial | Actual UI/MCP page control and native pixels/input on Worker |
| DW-17 | Gated helper claim, one-use bidirectional RFB relay, Back preserving browser reservation | Native helper, held-input release and return to Playwright on Worker |
| DW-18–22 | Durable uncertain claims, authenticated proof, verified-fence path and independent watchdog tests | Real network blackhole, service SIGKILL, host reboot and Master restart matrix |
| DW-23 | Authorized profile execution location is exposed; no promise of unproxied IP stability | Actual configured proxy egress and WebRTC on both hosts |
| DW-24 | Drain excludes new placement and permits current run commands/saving | Operational drain/rotation/re-enrollment on deployed service |
| DW-26 | Manifest and architecture-specific evidence validation | Independently measured native equivalence on x86-64 and ARM64 |

## Verified commands and results

Environment: Linux x86-64, Python 3.12.14. All tests use disposable fixtures.

| Command/check | Result |
| --- | --- |
| `python -m pytest -q -rs -m 'not browser' --maxfail=10` | **1181 passed, 60 skipped, 24 deselected**, eight warnings, 234.45 seconds; no failures |
| Actual local TLS/WSS WorkerAgent tests, included above | Both passed: authenticated command/disconnect handling and complete start/control/stop/HTTPS return/receipt/terminal-cleanup flow; native runtime is a fixture |
| `python tools/check_extension_compatibility.py` | PASS: **84 route/method entries, 93 hooks, 42 launch callsites** |
| `python tools/check_extension_compatibility.py --rehearse` | Disposable candidate rejected an unclassified route, missing shared admission hook, adapter-signature drift and an extra browser factory; restored candidate passed |
| `ruff check src tests chrome_engine foxprofile_ext tools/worker_preflight.py` | PASS |
| `ruff format --check src tests chrome_engine foxprofile_ext tools/worker_preflight.py` | PASS: 243 Python files formatted; final formatting preserved every affected parsed Python AST |
| `node --check --input-type=module` for the panel module and extracted inline web module | PASS for both JavaScript sources |
| `git diff --check` | PASS |

The full-run log has SHA-256
`94e99b66913578d7132307cd1564a101c248a34635c9d4cfe42d310fce8ce390`.
Scoped pass counts in individual review receipts overlap this run and must not
be summed. The package build/install checks are included in the full suite;
the final locked-checkpoint metadata/distribution selection
`python -m pytest -q tests/test_ext_compatibility_gate.py tests/test_ext_compatibility.py tests/test_ext_packaging.py`
passed **39 tests**, three warnings, in 37.01 seconds. It rebuilt and
installed the current package in isolation, including the final translated
Worker error labels. Its log SHA-256 is
`80bb985d168a172b72cc4a45aaf25cfca6ae9a02814ae5db8773050d857d749f`.

The 60 skips consist of 39 Chromium DOM checks, one real module check, 17
native-X harness cases, two opt-in bubblewrap integration cases and one
unavailable trusted resolver/confinement boundary. The 24 actual Chromium-engine
cases were excluded by the explicit `not browser` selection. No unconfined
fallback, global host-policy change or fabricated native evidence was used.
Warnings are dependency/runtime deprecations, an upstream settings annotation
warning and the intentional duplicate-ZIP rejection fixture.

The OpenSpec CLI is not installed in this environment, so a fresh CLI validation
is not claimed. Requirement/specification text was preserved; task checkboxes
record the implementation versus native/deployment evidence boundary.

## Independent review closure

The final read-only integration review found two confirmed issues: asynchronous
remote launch could capture stale metadata, and abnormal quarantine lacked a
durable unsaved indicator. Both are corrected with focused regression coverage.
The reviewer found no remaining confirmed blocker in the reviewed channel,
coordinator, configuration, HTTP, manual relay and narrow adapter scope. It did
not run native browser or deployed-host acceptance. The manual relay test's
client/server teardown scheduling race was corrected with a bounded wait for the
actual server revoke event, retaining both closure assertions.


## Rollout inputs and remaining acceptance

Use `deploy/extension/worker-master.env.example` together with the existing
protected Master configuration and `deploy/worker/README.md` for Worker setup.
Templates contain unresolved example values; production secrets, URLs, paths,
binary hashes and native evidence are operator-owned inputs.

Before rollout, execute DW-01–DW-26 on isolated Ubuntu 24.04 nodes, measure
1/5/10-browser RSS and headroom, verify native rendering/fonts/display/engine
equivalence, demonstrate proxy egress and full profile persistence, and exercise
network loss, service death, reboot, restore and verified fencing. Back up the
Master SQLite database, profile catalog and canonical profile directories as one
quiescent installation. Confirm Worker staging limits, secret ownership,
rotation, drain and removal. These items are retained in `openspec/TODO.md`.

## Publication verification

After normalization to the public runtime-source SHA, the same compatibility and
distribution selection passed again: **39 passed**, three warnings, 40.67 seconds.
The publication-gate log SHA-256 is
`1c328f27290db9adcd75abf1c49fc69953da89e336b702c02af73a984fc04cb0`.
The source gate again passed 84 routes / 93 hooks / 42 launch callsites.
An independent byte comparison confirmed unchanged executable and test files;
only the four checkpoint/evidence files differ from the verified local result.
This publication does not close native or two-host acceptance.
