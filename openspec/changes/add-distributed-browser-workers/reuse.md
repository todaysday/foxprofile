# Reuse and adaptation evidence

**Selected baseline:** todaysday/foxprofile, commit e8a9c204fce7e8fb50b5827977e0567f196cdfaf, branch codex/openspec-multi-user-manual-browser-workflow; code inspected through the connected GitHub repository. Project MIT license; no new external code is bundled by this OpenSpec change. Source tests below were inspected, not executed.

| Selected code or algorithm | Inspected behavior and test references | Adaptation |
| --- | --- | --- |
| foxprofile_ext/storage/capacity.py (CapacityLedger.reserve/claim/release_after_exit/recover) and storage/database.py | SQLite transaction reserves run_id/host_id, counts active runs; a partial unique index prevents two active rows per profile_id; uncertain process liveness blocks admission. tests/test_ext_capacity.py covers cross-process last slot, duplicate run and unknown liveness. | **Extend**, do not replace, with immutable remote ownership and per-host memory reservations. Current liveness probe is host-local. |
| foxprofile_ext/runtime/coordinator.py (reserve_and_start/status/stop/control), runtime/processes.py (RuntimeSupervisor), runtime/registry.py (RuntimeRegistry, process_identity) | Authorized single-host exact-run reservation, preparation, process start-time identity and scoped control. tests/test_ext_runtime.py covers races, stale generations, unknown processes, cancel and recovery. | **Adapt** via worker-owned exact-run adapter, preserving the single central authority. Foreign PIDs are never signaled by Master. |
| foxprofile_ext/runtime/supervisor.py, runtime/confinement.py; src/services/browser/display.py | Child-side gated launch and bubblewrap/Xvfb tracking, sanitized environment and strict worker-local process cleanup. Relevant tests test_ext_confinement.py/test_ext_runtime.py. | **Reuse** per Worker; add independent link watchdog and group/cgroup death proof. Do not bypass confinement to launch. |
| src/services/browser/control.py (PageController.dispatch/serve), src/services/browser/launcher.py | Existing loopback-token action API, screenshots, tabs and launch readiness protocol. tests/test_control.py, test_launcher.py. | **Reuse** action semantics; transport correlated requests, not loopback endpoint or credentials. |
| foxprofile_ext/manual/service.py (ManualSessionService), manual/backend.py (PrivateVNCBackend), manual/http.py (authenticated WebSocket RFB bridge) | One authorization-bound manual lease, exact display/runtime, input release, revocation and 60-second cleanup behavior. tests/test_ext_manual_service.py, test_ext_manual_http.py. | **Adapt** only host/backend transport; keep Master lease and current Camoufox support scope. |
| src/services/profile/transfer.py (export_to_zip/import_from_zip), foxprofile_ext/transfer.py (ManagedProfileTransfer) | Portable full profile ZIP and safety/authorization handling. tests/test_transfer.py, test_ext_transfer.py inspect traversal, identity and actor-bound handles. | **Reuse** safe walking/archive primitives, **not** ordinary user import/overwrite as the distributed commit; add revisioned staging and idempotent commit. |
| src/services/browser/fingerprint.py (load_or_create, IDENTITY_FILES), chrome_engine/persona.py (load_or_create) | Fingerprint/persona is loaded from persistent engine profile directory; normal runner generates only if missing/mismatched. tests/test_fingerprint.py and chrome_engine/tests/test_persona.py. | **Adapt** initial identity generation to Master; Worker must reject missing/mismatched identity instead of generating. |
| foxprofile_ext/transport.py (websocket_transport_options), docs/EXTENSION_DEPLOY.md, deploy/extension | Protected WebSocket keepalive and one-installation pinned protected deployment; no remote Worker registration in those paths. | **Reference/adapt** transport and preflight. Worker is a distinct role, not a second Master with shared SQLite. |

## Demonstrated gaps

The inspected RuntimeCoordinator owns local host_id; RuntimeSupervisor uses local PIDs, TCP loopback PageController and local X display; transfer handles user-managed portable ZIP rather than a remote revisioned commit. These paths cannot by themselves register remote Workers, place runs by available RAM, synchronize canonical revisions after a remote stop, or prove foreign-host cleanup. New small extension-owned placement/transport/sync facilities are needed. Avoid a second profile manager, process-control implementation, VNC backend or broker.

## External references inspected

- Linux proc documentation defines **MemAvailable as an estimate**, not an exact resource promise: https://www.kernel.org/doc/html/latest/filesystems/proc.html .
- SQLite documentation describes unique partial indexes; the fork already uses this pattern: https://www.sqlite.org/partialindex.html .
- systemd service watchdog and restart documentation: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html .
- systemd process cleanup and control group semantics: https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html .

These external references are algorithm/operational guidance, not imported code. They do not prove target VPS configuration; native validation is deferred to implementation and deployment acceptance.
