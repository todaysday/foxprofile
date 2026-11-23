# Source boundary, inspected behavior, and verification limits

## Original requirements and chronology

Owner, 2026-11-23: single Master stores profiles/fingerprint/cookies and monitors RAM/connected VPS; when memory is short, route a run to available Worker, never run one profile on two hosts; Worker deployed externally, reports ready, receives copy/launch parameters, exposes session control back to Master, returns updated data on normal stop, kills browser on communication loss (last Master copy preserved, recent changes may be lost).

Owner later clarified: all servers use **Ubuntu 24.04** and other runtime components are installed centrally; no need for a per-profile OS compatibility matrix. Owner requested OpenSpec in the **existing** implementation branch codex/openspec-multi-user-manual-browser-workflow rather than a separate branch. This clarification supersedes any earlier proposal to work in a new branch. These are design decisions, not proof that all WebGL/font/browser values are identical in deployed systems.

## Selected code revision

Repository: https://github.com/todaysday/foxprofile
Immutable commit: e8a9c204fce7e8fb50b5827977e0567f196cdfaf (2026-11-09).
Other branches checked: main at 530b993ff3a7d211aaa0904fba0817b77c75d4c9 (2026-10-30); three dependency-only Dependabot branches. Selected implementation branch is 46 commits ahead of main, with foxprofile_ext and active OpenSpec.

Relevant implementation paths inspected at selected revision:
- foxprofile_ext/storage/capacity.py: reserve, claim, release_after_exit, recover, per-profile partial unique active-run index; storage/database.py migrations.
- foxprofile_ext/runtime/coordinator.py: authorized reserve_and_start, status, stop, control; runtime/processes.py: local RuntimeSupervisor; runtime/registry.py: local host/process verification; runtime/supervisor.py and runtime/confinement.py for gated sandboxed child launch.
- foxprofile_ext/manual/service.py: principal/run-bound manual lease; manual/http.py private RFB bridge; manual/backend.py existing x11vnc helper; manual/contracts.py lifecycle responsibilities.
- src/services/profile/transfer.py: portable archive; foxprofile_ext/transfer.py: managed archive handles; src/services/browser/fingerprint.py and chrome_engine/persona.py: persisted identity.
- src/services/browser/control.py: Playwright dispatch and loopback control; src/services/browser/launcher.py and process.py: runner readiness/process lifecycle.
- foxprofile_ext/transport.py and docs/EXTENSION_DEPLOY.md: existing WSS and one-manager deployment constraints.

Tests inspected (NOT RUN): tests/test_ext_capacity.py, test_ext_runtime.py, test_ext_manual_service.py, test_ext_transfer.py; baselines tests/test_launcher.py, test_control.py, test_fingerprint.py, test_transfer.py. Test names and source assertions are evidence of existing intended contracts, not proof of runtime behavior on target hosts.

Existing OpenSpec: openspec/changes/add-multi-user-manual-browser-workflow; its slice 09 live acceptance remains open in tasks.md and openspec/TODO.md. Existing specifications limiting manual lease to one installation and no cross-host pool describe the *previous* scope; this additive change explicitly extends execution to remote hosts without multiplying Masters or manual session leases.

## External discovery

Official Linux kernel /proc docs for MemAvailable, official SQLite partial-index constraints and official systemd service/kill documentation were consulted as bounded algorithm and deployment references. No third-party worker broker, distributed database, or alternate browser engine was adopted: the selected fork already has durable run identity, SQLite exclusion and browser-control bridges, and only small remote adapters are needed. No external source code is copied.

## Remaining evidence gaps

- No code for the distributed worker capability has been implemented or tested.
- No deployment result establishing identical WebGL, renderer, fonts or browser-visible geometry across target Ubuntu 24.04 hosts was observed.
- No benchmark for browser RSS/headroom or Worker staging transfer sizes was performed.
- No live loss-of-connectivity or same-profile cross-host concurrency trial was run.
- Existing live slice-09 acceptance is still incomplete and must not be relabeled as passed.

Source inspection and documentation are not test execution, deployment readiness or permission to alter production host settings.
