# Protected extension deployment handoff

This is the offline slice 8.3 handoff. **Both real hosts: BLOCKED; slice 09: NOT RUN.**
Host addresses, access and actual topology have not been supplied. Synthetic tests
and local native fixtures do not establish ARM64 or x86-64 host acceptance. A
separate deployment agent executes [slice 09](../openspec/changes/add-multi-user-manual-browser-workflow/slices/09-live-deployment-and-acceptance.md)
and records every required live case and resource measurement. Keep existing
[DEPLOY.md](DEPLOY.md) for the upstream deployment; protected mode uses this handoff.
[Tiếng Việt](EXTENSION_DEPLOY.vi.md).

## Locked inputs and topology

Deploy an exact fork commit plus its matching built distribution and dependency
lock/artifacts. `foxprofile_ext/compatibility.json` describes the tested extension
contracts; it is not proof that a target host passed. Install the distribution and
`requirements-extension.txt` in a dedicated environment. Record `python -m pip
freeze --all` privately as `dependencies.txt`; retain the corresponding dependency
wheels/environment or image by digest, Python version, browser builds and native
packages. The report alone cannot recreate an unavailable dependency. Do not
download dependencies during rollback.

Use exactly one lifecycle manager and one ASGI worker per installation, with the
same-origin panel/API/WebSocket routed to that manager. Sticky routing is required
if a proxy has more than one upstream installation; it does not enable sharing a
dataset across managers. Shared SQLite admission does not provide cross-worker
backend IPC. Do not use `uvicorn --workers`, gunicorn multi-worker mode or a second
desktop manager against the same installation. Default global capacity is 10;
preserve an existing valid capacity. Lowering capacity never means killing runs.

The files under [deploy/extension](../deploy/extension) are templates, **not
discovered service definitions**. Copy `target.json.example` into a private
operator directory; resolve every `UNRESOLVED` field for each host. Replace all
`LOCKED_COMMIT`/`REPLACE_...` placeholders and resolve owners, actual data paths,
TLS, service/container topology, namespace and resource policy before use.

## Configuration and native prerequisites

Install private `runtime.env` and `secrets.env` from the templates, mode 0600,
inside the service-owned configuration directory (0700). Set
`FOXPROFILE_EXT_ENABLED=true`, `FOXPROFILE_EXT_DB`, `FOXPROFILE_DATA_DIR`,
`FOXPROFILE_PROFILES_FILE` and **`FOXPROFILE_HEADLESS=virtual` before Python imports
`src.core.config`**. The systemd EnvironmentFiles do this. Never unset protected
mode to get past failed startup or restore.

Create the service credential directly in the private secret file through the
existing secret manager or a local terminal. For a new isolated installation,
this example creates a new file exclusively, prints no token, and refuses to
overwrite an existing credential:

```bash
python - <<'PY'
import os
import secrets
path = "/etc/foxprofile-extension/secrets.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    output.write("FOXPROFILE_API_TOKEN=" + secrets.token_urlsafe(36) + "\n")
PY
```

Run with appropriate ownership/permissions; the service must read this file.
Keep existing credentials when migrating or rolling back. No token in process
arguments, URLs, shell history, screenshots or acceptance reports. Personal panel
users receive personal accounts/sessions; the service credential stays server-side.

Install and verify Camoufox and Chrome builds **before startup**; configure their
actual installed executable paths. An optional engine can be omitted when unused.
Protected runtime discovery does not implicitly download a missing browser.
Install bubblewrap (`bwrap`), Xvfb, xauth and x11vnc using the host's native package
system. Verify user namespace/confinement support in staging. Failure must remain
protected; do not weaken confinement to start a browser.

Set `FOXPROFILE_X11VNC_EXECUTABLE` to the exact absolute executable and
`FOXPROFILE_X11VNC_SHA256` to its **independently verified** SHA-256. Observed offline
amd64 distro build `0.9.17-1` had digest
`249bdc17e09fd96ea73ca7fbec2d8f3cd9ff411d0bbfb634d99431548fb77132`.
This observation is not automatic trust for another binary or host. ARM64 digest
is unknown; the deployment agent verifies and records it rather than copying the
amd64 value. Missing/mismatched adapter settings deny manual access.

Browser, runner-owned Xvfb, Xauthority and temporary VNC helper must share the
same service/container private temporary namespace. A matching DISPLAY string
alone is insufficient. Keep `PrivateTmp=true` only with this shared child topology;
do not attach a VNC process from another service or add another Xvfb. noVNC
supports the existing virtual Camoufox run; Chrome manual access is unsupported.

Use HTTPS/WSS and same-origin cookies. The nginx template forwards WebSocket
Upgrade with 90-second proxy timeouts. Application keepalive detects dead peers
using 10-second Ping plus 10-second timeout; the remaining cleanup budget is
40 seconds, total at most 60 seconds. Proxy timeouts are not the cleanup clock.
No public VNC listener or permanent credentials in access URLs.

## Consistent backup, migration and restore commands

`python -m foxprofile_ext.maintenance --help` is packaged with the extension.
Its three commands never start browsers or contact hosts. Fill a private copy of
`installation.json.example` with absolute paths and the exact 40-hex release
commit. All six file/directory roots must be separate and non-overlapping.
`release_dir` contains the immutable expanded application distribution, **not** a
checkout with `.git`, a virtualenv or links; preserve separately pinned executable
dependency/browser/native artifacts for that release. `configuration_dir`
contains the complete service configuration including private environment files
and actual unit/proxy configuration copies. Keep the descriptor outside that
configuration directory if it describes a different restored layout.

Before backup/migration, fence admission at the real proxy/service boundary,
close browsers normally, verify every profile browser/helper/display has exited,
then stop the owning manager. Verify no second manager or cookie helper can use
the directories. Do not terminate profiles just to meet a lower capacity setting.
`--stopped-and-closed` is an explicit operator attestation: a filesystem copy
cannot prove process absence on its own. The command additionally holds the
shared `profiles.lock` (10-second acquisition deadline), refuses active/reserved/
uncertain runs, blocked recovery, outstanding runtime descriptors and open manual
leases, and rejects browser lock markers. Do not edit the DB or remove markers to
silence those checks: reconcile with the owning manager and confirm shutdown.

Run as the service data owner in a private maintenance directory (`umask 077`).
Examples use placeholder paths; resolve them first:

```bash
umask 077
python -m foxprofile_ext.maintenance backup \
  --installation /srv/foxprofile-maintenance/installation.json \
  --stopped-and-closed \
  --destination /srv/foxprofile-extension-backups/before-change

mkdir -m 700 /srv/foxprofile-extension-staging
python -m foxprofile_ext.maintenance restore \
  --snapshot /srv/foxprofile-extension-backups/before-change \
  --empty-disposable-destination /srv/foxprofile-extension-staging

python -m foxprofile_ext.maintenance migrate \
  --installation /srv/foxprofile-extension-staging/installation.json \
  --stopped-and-closed
python -m foxprofile_ext.maintenance migrate \
  --installation /srv/foxprofile-extension-staging/installation.json \
  --stopped-and-closed
```

Backup atomically publishes a directory containing SQLite's backup-API snapshot,
catalog, all profile directories (including residual/quarantined data), matching
configuration, release and dependency report. A pre-extension install can have
no database; the manifest records that absence. File/directory inventory, SHA-256
and executable-bit checks detect accidental changes. This is a private local
snapshot, not a signed archive or a backup of a running browser. Symlinks,
hard-linked files, devices and FIFOs are rejected; there is no archive extraction
or caller-controlled archive path to traverse. Inputs must remain immutable
during maintenance. Keep snapshots confidential: they contain cookies, proxy
credentials, service secrets and password/session hashes. Never attach them to
git, reports or a public artifact store.

Restore checks layout/inventory, rejects unsafe paths and active/uncertain state,
copies into a private sibling staging directory, then replaces only the explicit
**existing empty disposable destination**. It never overwrites a production tree.
Restored directories are 0700 and files 0600, retaining the owner's executable
bit. Run as the service owner to retain that ownership; record/reapply any required
host-specific ACL/labels deliberately before startup. It does not preserve
arbitrary ACLs, xattrs or foreign ownership. The generated `installation.json`
targets the restored copy. Original configuration is preserved byte-for-byte:
**do not source it unchanged in staging**, where its paths may point at production.
Create a private staging environment/unit with separate data/DB/profile/log paths,
installation/runtime identity, listeners and namespace. Keep the original matched
configuration in the snapshot for rollback.

Migration uses the existing schema transaction, `ProfileCatalog` stable-ID
migration and finite pending-operation recovery. It preserves existing IDs,
grants and a valid saved limit; absent capacity becomes 10. Invalid saved capacity
fails instead of resetting it. Browser data is not rewritten. An interrupted
schema transaction rolls back and can be rerun; interrupted catalog migration
uses the existing intent ledger. Pending profiles remain unavailable until
recovery completes. The synthetic tests execute interruption before/after catalog
save, schema rollback, reruns and matched restore; these do not verify real login
preservation.

Only after staging migration succeeds, initialize the first admin locally using
the same explicit staging DB (interactive password prompt, no password argument):

```bash
python -m foxprofile_ext.bootstrap \
  --database /srv/foxprofile-extension-staging/state.sqlite3 --username admin0
```

Existing administrators make bootstrap refuse a second initialization. After
staging acceptance and a fresh production backup, execute `migrate` against the
resolved **production** descriptor while that installation is stopped, then start
only the locked protected release with its matching environment.

## Health, acceptance and matched rollback

Syntax-check the resolved service file with `systemd-analyze verify` and the
resolved nginx configuration with `nginx -t`. Placeholders, absent certificates,
service users or executable paths must be resolved; template parsing is not host
validation. Start a separate staging instance and check `curl --fail
http://127.0.0.1:8000/health` and `/info` without credentials (public, loopback).
Then check login/session flow, unauthenticated protected-route denial, assignment
filtering, service separation and saved capacity. Public health alone does not
prove authorization. Use a secret-manager client or a private request-header
file for authenticated checks; never put a token in `curl -H` arguments.

The deployment agent executes the full [acceptance plan](../openspec/changes/add-multi-user-manual-browser-workflow/references/acceptance-plan.md)
on both hosts, including real browser/proxy/login/fingerprint preservation,
native-dialog/environment confinement, same-run input/Back/cleanup, failure
injection and 1/5/10-browser resource measurements. Record actual results as
PASS/FAIL/BLOCKED/NOT RUN. Missing host evidence remains BLOCKED/NOT RUN.

For rollback, fence external admission, close owned browsers and stop the failed
manager; preserve private failure evidence. Use the same `restore` command with
the prior snapshot and a **new empty disposable destination**. Validate its
matching code/config/data/dependency identity, restore the pinned executable
dependencies and browser/native builds without downloads, then retarget the
service to that tested protected dataset/release/configuration together. Verify
authentication, IDs/grants, capacity, fingerprint/login and ordinary controls
before reopening access. Never copy only SQLite/catalog, promise a schema
downgrade, run old unprotected upstream over managed data or disable the
extension as a fallback. If the prior snapshot predates protected mode, it is a
private recovery source for a new protected staging migration; it is not an
unprotected public rollback target.

Offline verification:

```bash
python -m pytest -q tests/test_ext_maintenance.py
ruff check foxprofile_ext/maintenance.py foxprofile_ext/locking.py tests/test_ext_maintenance.py
```
