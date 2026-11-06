# Disposable extension acceptance fixture

[Tiếng Việt](EXTENSION_ACCEPTANCE.vi.md) · [Deployment](EXTENSION_DEPLOY.md) · [Authoritative cases](../openspec/changes/add-multi-user-manual-browser-workflow/references/acceptance-plan.md)

`python -m foxprofile_ext.acceptance` supplies three small commands: offline fixture preparation, a loopback input page, and explicit staging HTTP smoke checks. It never discovers hosts, downloads or launches browsers, configures an agent, calls CAPTCHA services, resets a server, or deletes profiles. Production rollout remains the deployment agent's responsibility.

## Prepare offline

Run from the locked release's repository root, using its Python environment with runtime and extension dependencies installed. Select an isolated local staging directory whose parent already exists. Never select production storage or a restored production browser directory for this disposable fixture.

Supply `--credentials-file` pointing to an operator-owned, single-link regular JSON file with mode `0600`; symlink paths are rejected. Create/edit this file privately, outside the repository. The following values are **fake examples**, not defaults or real credentials:

```json
{
  "admin0_password": "example-admin-password",
  "a_password": "example-a-password",
  "b_password": "example-b-password",
  "service_token": "example-service-token-at-least-24-characters"
}
```

Replace every value privately. The service token needs at least 24 ASCII characters. Omit `--credentials-file` to enter all four values through masked interactive terminal prompts. No password or service-token command-line flags exist.

```bash
python -m foxprofile_ext.acceptance prepare \
  --destination /tmp/foxprofile-acceptance \
  --credentials-file /tmp/acceptance-credentials.json
```

The destination must be absent or empty. Symlinks in its path are rejected; a nonempty destination is never overwritten. Preparation does not contact any target or network. It uses the actual `ProfileManager`, `ProfileCatalog`, account/password-hashing DAOs, assignment DAO, settings DAO and extension migrations. Nothing launches a browser or generates a browser fingerprint.

Outputs have private directory/file permissions (`0700`/`0600` on POSIX):

| Output | Content |
| --- | --- |
| `fixture.json` | Unique fixture ID, persisted user/profile IDs, names, expected capacity |
| `runtime-env.json` | Explicit isolated storage paths, loopback host, virtual headless mode, **private service token** |
| `foxprofile_ext.sqlite3` | admin0 administrator, A/B ordinary accounts, hashed passwords, assignments, saved capacity 10 |
| `profiles.json`, `browserdata/` | Eleven harmless Camoufox/Linux profiles, no proxy, cookie, login or fingerprint |

A owns PA, B owns PB; nine other profiles are unassigned and administrator/service-visible. Names include the fixture ID prefix. IDs stay stable in the persisted fixture; a separately prepared fixture has different identities. No service user account or reusable personal session is manufactured. The service credential belongs only to server configuration. Plaintext passwords remain exclusively in operator input.

## Start isolated staging explicitly

First complete [deployment preflight](EXTENSION_DEPLOY.md), including locked release/dependency checks, separate installation/process state, actual host architecture, tested browser/adapter builds, confinement, private namespace and protected listener configuration. `runtime-env.json` describes fixture storage, not a complete deployment manifest. Merge the reviewed staging settings through the existing private configuration mechanism. Do not copy production settings or credentials blindly. Missing actual hosts, ARM64 hardware, browser build digests or adapter values mean deployment preflight **BLOCKED**, never invented values or a passing target result.

After preflight, this explicit local launch loads the private fixture environment before application imports:

```bash
python - /tmp/foxprofile-acceptance/runtime-env.json <<'PY'
import json
import os
import runpy
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    os.environ.update(json.load(source))
sys.argv = ["src.server", "--host", "127.0.0.1", "--port", "8800"]
runpy.run_module("src.server", run_name="__main__")
PY
```

Keep the server restricted to this isolated installation. A reverse proxy, when used, must supply the reviewed HTTPS/WSS configuration. This command does not launch a profile. Stop the server explicitly after staging work; the acceptance helper does not manage its lifecycle.

## HTTP smoke on the explicitly selected stage

Read the fixture ID from the local `fixture.json`. Supply it deliberately with the staging origin:

```bash
python -m foxprofile_ext.acceptance smoke \
  --fixture /tmp/foxprofile-acceptance \
  --expect-fixture FIXTURE_UUID_FROM_LOCAL_MANIFEST \
  --base-url http://127.0.0.1:8800 \
  --credentials-file /tmp/acceptance-credentials.json
```

Public origins require HTTPS. URLs with credentials, paths, query strings or fragments are rejected. Redirects and environment HTTP proxies are disabled. The helper creates three real personal sessions and uses a separate service HTTP client. It checks user IDs/roles and the exact eleven-profile disposable catalog, then A/B filtering, foreign ID/name denials, ordinary-user settings denial, service visibility, service/personal-session separation, and administrator capacity read. A wrong fixture confirmation fails before HTTP; wrong target IDs/catalog fail before access assertions. Target identity requires logging in, so only this command's login/logout session rows may change. It never creates, starts, stops, updates or deletes target profiles, users, grants or settings. A target containing extra profiles is rejected rather than treated as disposable.

The four printed `PASS` groups cover this narrow smoke only. Failure output omits request URLs, credentials, cookies and response bodies. Keep operator credential/configuration files private; do not attach them to reports.

## Controlled browser/noVNC input

Serve only the bundled page, explicitly, on the browser host:

```bash
python -m foxprofile_ext.acceptance serve-page --port 8765
```

Only `127.0.0.1` is bound. `/` and `/acceptance-input.html` return the page; other paths return 404. No directory listing, agent, CAPTCHA, telemetry, external asset or network request is included. Its CSP blocks outbound connections. Browser network namespaces must have a reviewed way to reach this loopback origin; do not widen the listener to make an unreviewed topology work. For a remote host, use the reviewed loopback forwarding arrangement. Stop this command with Ctrl+C.

Using A's PA and B's PB from `fixture.json`, execute the authoritative A01–A30, N01–N13, S01–S03 and X cases separately. The page supports these manual observations:

1. Explicitly start PA through authorized ordinary controls and navigate it to `http://127.0.0.1:8765/`. Record run identity, tabs, display framebuffer/window geometry and page `#geometry` before manual access. Its values include viewport, outer window, screen/available screen, DPR and scroll position; native window position/framebuffer need host tools.
2. From PA's existing Viewer open noVNC. Type in `#text-input`, click `#ordinary-click`, drag `#drag-source` into `#drop-target`, scroll `#scroll-box` and the outer page. Inspect text, click/drop counts and scroll position directly. No automatic success decision is emitted.
3. Exercise held keys/buttons during each authorized Back/disconnect/timeout case. Inspect `#held-input` and recent key/button events; blur deliberately retains observed state. After returning, use ordinary Playwright/API control to click `#next-click` and read `#next-click-count` and the event log. The existing endpoint is `POST /api/v1/browser/id%3A{PA_ID}/page/click` with `{"selector":"#next-click"}`, using an authorized session plus Origin/CSRF or the private service client. Here `PA_ID` is the raw UUID from `fixture.json`; `id%3A` encodes the unambiguous `id:` locator prefix for this legacy route. Canonical extension `profile_id` paths and `profile_ids` bodies still use raw UUIDs. The page never sends this request itself.
4. Compare the same run/tabs and geometry before/during/after; verify PB and other neighbors are unaffected. Reopen/close repeatedly and measure owned VNC/display/browser process counts and the end-to-end cleanup timer required by each case.
5. Repeat with no agent configured and with an independently waiting agent. FoxProfile must not send messages, judge completion or call pause/resume. The user communicates readiness outside FoxProfile.

Do not treat retained DOM markers as proof that the OS released every key/button. Inspect host input/process state and the actual next ordinary click as the source cases require. This helper exposes no public crash, failure-injection or reset endpoint. Existing offline supervisor/manual-service tests exercise deterministic failure injection; live parent/gateway crashes, capacity races, native file/proc canaries, restart/persistence, proxy/WebRTC checks and host load tests require the deployment runbook's disposable-host procedures and evidence.

## Evidence boundary

`pytest tests/test_ext_acceptance_fixture.py -q` validates offline preparation, private permissions/refusals, real users/sessions/grants/catalog/settings, actual loopback HTTP serving, and the smoke against the real disposable application routes over local HTTP. It uses no downloaded browser or real VNC adapter. This is offline handoff evidence only.

Actual target execution, all 30 access rows, all 13 noVNC rows, all three per-host stealth rows, crash/recovery/isolation cases and ARM64/x86-64 resource measurements remain **NOT RUN** until individually executed and recorded. Missing target prerequisites are **BLOCKED** in deployment preflight. Neither an offline test pass nor these four HTTP smoke groups changes those case statuses automatically.
