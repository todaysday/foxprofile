# FoxProfile Worker deployment

This template deploys an outbound-only browser execution process on Ubuntu 24.04. Worker receives exact Master-issued run IDs and run-scoped profile snapshots. It has no Master catalog, users, grants, client API, MCP endpoint, public VNC listener, or independent admission path. Browser commands, snapshots, clean-stop uploads and manual RFB relays all originate from the Worker and connect to Master over authenticated TLS.

The files here are templates. They do not prove a host is compatible or accepted for production. The distributed change remains unaccepted until the separate two-host browser, identity, persistence and loss tests in `openspec/changes/add-distributed-browser-workers/references/acceptance.md` pass. Existing live acceptance blockers in the multi-user workflow remain separate.

## Install one Worker

Install the locked FoxProfile distribution, Python dependencies, Camoufox/Chrome builds, fonts, fontconfig, bubblewrap, Xvfb and xauth before enabling the service. Install each selected native package from the approved Ubuntu repository or the deployment's pinned artifact set. Do not use browser-time package downloads. Install optional x11vnc only when its exact binary digest has been independently pinned.

Create a dedicated `foxprofile` service account. Install the locked application at `/opt/foxprofile`, the virtual environment at `/opt/foxprofile/.venv`, and the service template at `/etc/systemd/system/foxprofile-worker.service`. Resolve executable paths and ownership for the actual host. Keep the application and browser runtimes read-only; `/var/lib/foxprofile-worker` is the private writable state directory.

Create `/etc/foxprofile-worker` as a root-owned directory with mode `0700`. Copy `worker.env.example` to `worker.env`; set its Worker ID, HTTPS Master origin as `wss://...`, runtime manifest paths, expected manifest digest and measured resource reserves. This file contains configuration only. Set mode `0600` and do not place any token in it.

Create `/etc/foxprofile-worker/worker-token` with the unique credential enrolled for this `worker_id`, using the deployment secret manager or a private terminal. Keep it root-owned and mode `0600`. systemd injects it through `LoadCredential`; the agent reads it from `CREDENTIALS_DIRECTORY`. Never add the credential to command arguments, environment files, URLs, logs, browser settings or profile data. Before rotation, drain the Worker and cleanly stop/save its current runs. Rotation immediately invalidates the previous credential and disconnects its control channel. Replace the credential file, restart this unit and verify the new authenticated connection. Do not call the revoke route afterward: it revokes the Worker enrollment, including the current credential.

Install the runtime manifest and host-specific native evidence as root-owned, read-only JSON files. Their contents must come from the locked runtime and a real browser run on the exact host. Worker preflight deliberately rejects missing or `NOT_RUN` evidence. A report must cover every installed engine and include `runtime_pins_sha256`, the SHA-256 from `runtime_pins_digest` over the exact measured Python version, binary paths and hashes, package versions and font inventory in the selected architecture entry.

First collect the exact software and font pins without making a native compatibility claim:

```bash
cd /opt/foxprofile
python tools/worker_preflight.py --measure-pins /etc/foxprofile-worker/measured-pins.json
```

This writes a private `MEASURED_ONLY` record and prints `runtime_pins_sha256`. Use those measured pins when running the external native acceptance procedure. The command does not create a `PASS` report, renderer result, identity result, sandbox result or proxy result.

## Runtime manifest and native evidence

`runtime-manifest.example.json` is an invalid skeleton: it has no runtime pins, so a Worker cannot connect with it. Generate a shared manifest only after measuring the approved runtime on the host. Supply native evidence with this shape:

```json
{
  "format_version": 1,
  "status": "PASS",
  "architecture": "<measured architecture>",
  "runtime_pins_sha256": "<measured runtime pins digest>",
  "engines": {
    "camoufox": {
      "checks": {
        "browser_geometry": true,
        "webgl_renderer": true,
        "rendering_backend": true,
        "font_fingerprint": true,
        "identity_roundtrip": true,
        "sandbox": true,
        "network_proxy": true
      },
      "geometry": {"screen_width": "<measured integer>", "screen_height": "<measured integer>"},
      "webgl_renderer": "<measured browser renderer>",
      "rendering_backend": {"api": "<measured>", "vendor": "<measured>", "renderer": "<measured>", "version": "<measured>", "software": "<measured boolean>"},
      "fonts_sha256": "<measured installed font digest>",
      "identity": {"stable": true, "same_bytes_after_roundtrip": true},
      "sandbox": {"bubblewrap": true, "user_namespace": true, "pid_namespace": true, "profile_mount_isolated": true},
      "network_proxy": {"profile_proxy_honored": true, "proxy_failure_no_direct_fallback": true, "webrtc_policy": "<measured policy>", "dns_policy": "<measured policy>"}
    }
  }
}
```

All angle-bracketed values above must be replaced by measured facts; the structure is not valid evidence. Include a matching `chrome` entry when Chrome is installed. Record actual dimensions and WebGL renderer observed through each browser, actual graphics API/vendor/driver/backend, installed font inventory digest, exact host architecture, same-profile identity byte stability, real bubblewrap namespace/profile isolation, and proxy failure behavior. Keep each host's evidence private to that host. The generator hashes actual binaries, Python packages and installed font files; it does not invent renderer, backend, network or identity results.

To add an architecture entry to the shared manifest, run this command after native evidence exists:

```bash
cd /opt/foxprofile
python tools/worker_preflight.py \
  --generate \
  --manifest /etc/foxprofile-worker/runtime-manifest.json \
  --native-evidence /etc/foxprofile-worker/native-evidence.json
```

Repeat on each supported architecture against the same private manifest, then distribute the final identical manifest and digest to every Worker. The generator rejects differing measured geometry, renderer, font inventory or package versions across architectures. If actual native outputs differ, stop and investigate; do not edit evidence to force a pass.

Verify on every host before service start:

```bash
python tools/worker_preflight.py \
  --manifest /etc/foxprofile-worker/runtime-manifest.json \
  --native-evidence /etc/foxprofile-worker/native-evidence.json \
  --expected-sha256 "paste-the-shared-manifest-sha256-here"
```

Set the reported shared digest in `worker.env`. The preflight checks Ubuntu 24.04, CPU architecture, Python version, pinned executable hashes, package versions, installed fonts and the host's measured native evidence. A missing binary, wrong digest, absent engine or mismatched evidence keeps the Worker ineligible.

## Master administrator operations

Use the existing authenticated personal administrator session on Master. Cookie
mutations require the existing CSRF header. A trusted service token and an ordinary
personal account cannot perform these operations. Enrollment and rotation return
the credential once; store it directly in the private systemd credential file.

| Operation | Master route | Body / result |
| --- | --- | --- |
| Inventory | `GET /api/v1/workers` | Redacted Worker status, capacity and assigned runs |
| Enroll | `POST /api/v1/workers/{worker_id}/enroll` | Returns the unique credential |
| Drain/resume | `POST /api/v1/workers/{worker_id}/drain` | `{"draining": true}` / `{"draining": false}` |
| Rotate | `POST /api/v1/workers/{worker_id}/rotate` | Immediately invalidates the previous credential; returns its replacement |
| Revoke | `POST /api/v1/workers/{worker_id}/revoke` | Disables the enrollment and disconnects it |

Inventory and drain/resume are also available in the existing web and desktop
Administration dialogs. An uncertain run requires exact stop evidence or the
separate verified-host-fence operation; deleting credentials never proves process
death. The fence route is `POST /api/v1/workers/runs/{run_id}/fence`, with the exact
`profile_id`, `host_fenced: true` and a nonempty `evidence` record. Use it only after
independently verifying that the whole old host has stopped; it does not power off
the host itself.

## Start and operate

Run `systemd-analyze verify /etc/systemd/system/foxprofile-worker.service`, then enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now foxprofile-worker.service
sudo systemctl status foxprofile-worker.service
```

The Worker opens outbound WSS to `/api/v1/workers/connect`, sends 10-second heartbeats, and accepts commands only for the connection generation returned by Master. Snapshot GET and upload POST use run-scoped HTTPS capabilities. There is no Worker listener to expose at a firewall. Keep outbound HTTPS/WSS routing to the configured Master, and do not mount a shared live profile directory.

The unit uses `KillMode=control-group`, a 90-second systemd watchdog, a 70-second stop budget and an independent lease watchdog. The independent watchdog is armed before any browser spawn; a verified Master heartbeat acknowledgment renews it. After loss, it sends TERM and then KILL to exact owned process groups, stores only verified stop proofs, and terminates the agent. The independent watchdog also catches systemd's stop signal and completes its bounded cleanup before the unit stop deadline; systemd then cleans any remaining child processes in the unit cgroup. A PID disappearing by itself is never recorded as browser-tree proof. Unknown process state remains blocked for Master recovery.

Use Master administrator controls to drain a Worker before maintenance. A drained Worker accepts no new placements; cleanly stop active profiles through Master so their current browser data uploads first. After Master durably closes a run as committed or quarantined, it sends an exact-run cleanup acknowledgment over the same Worker boot's authenticated channel. The Worker then removes only that isolated run's profile bytes and transfer archive while retaining the recent stop proof/receipt window. If the Worker cannot reconnect on the same OS boot, keep its state directory for reconciliation. After updating a runtime or rotating credentials, verify the Worker returns with the expected manifest digest and fresh heartbeat before removing the old host. Do not clear Worker state or delete a Worker entry to resolve an uncertain run; use exact stop proof or verified host fencing.

## Manual browser access

Remote manual access is supported only for an existing Camoufox run when the pinned x11vnc binary and digest are configured. Set `FOXPROFILE_X11VNC_EXECUTABLE` and `FOXPROFILE_X11VNC_SHA256` in `worker.env` only after verifying the exact installed binary. Worker attaches the helper to the same confined display and Xauthority as the browser, then opens a short-lived outbound binary WSS relay supplied by Master. Chrome manual access remains unsupported. Never open a VNC port on the Worker.

## Remove a Worker

Drain the Worker in Master, stop or safely recover its active runs, revoke its enrollment credential, then stop and disable `foxprofile-worker.service`. Preserve the Worker runtime database, stopped-run proofs and staged archives until Master confirms the matching runs are closed. Remove the systemd credential and state directory only after that reconciliation and the required private backup.
