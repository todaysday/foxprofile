# Implementation handoff

**Updated:** 2026-11-07. **Scope:** implementation slices 1–8; separate deployment agent owns [slice 09](../slices/09-live-deployment-and-acceptance.md). **Release status: slices 1–8 complete; offline verification, immutable artifact lock, independent review and separate deployment-agent handoff accepted.** No real server has been deployed, migrated or accepted by this work. Both target hosts/access are unresolved; all live A/N/S/X cases and resource measurements remain **NOT RUN**.

This is the canonical implementation evidence and handoff record. [tasks.md](../tasks.md) tracks completion; [acceptance-plan.md](acceptance-plan.md) retains the acceptance inventory and live result cells. [VALIDATION.md](../../../../VALIDATION.md) records document checks, not product acceptance. Do not create a second implementation ledger or archive this change before slice 09 closes its required cases.

## Subsequent fork code review

The uncapped `xhigh --fix` review recovered all 51 original subagent reports, independently checked the correction groups, and preserved upstream geo-normalization behavior after an integrated regression exposed an overstrict new import check. Reviewed source **`2e42a93e0b4441511ec4516941cc0217cc31a898`** passed **1110 tests, 24 actual-engine browser cases deselected, three warnings, no failures or skips**, 385.96 seconds. This used its pristine Git archive, Python 3.12.3, native X fixtures and isolated Playwright Chromium. A private mount namespace supplied a trusted resolver fixture; the host resolver and production guard were unchanged. All 402 tracked archive blobs still matched Git after the run. The full-run log SHA-256 is `40b8d97bcf259f990d752e0cd8f1d04b131539b8b2fefbf929568a69c5e2a59d`; raw receipts remain outside Git.

Root `completion.review` records the findings, original agent continuity, failed first integrated attempt, corrective checks and remaining limitations. The compatibility register identifies this reviewed runtime-source checkpoint; later audit metadata does not change its Python source. Distribution and deployment-agent receipts below remain historical. Refresh release artifacts before any future deployment; this review performs no live operation or archive and does not close slice 9.

## Release identity and integrity

The release and distribution rows below describe the earlier implementation checkpoint. Subsequent fork-review corrections supersede that source; the review receipt in root `completion.review` identifies the revised candidate and its executed checks. Earlier artifact hashes and test counts must not be attributed to the revised source. No live acceptance or archive is established by this code review.

| Input | Observed identity / release status |
| --- | --- |
| Audited upstream | `530b993ff3a7d211aaa0904fba0817b77c75d4c9`, FoxProfile 2.4.0; actual installed host revision unknown. |
| Locked runtime-source checkpoint | `e959a26e11c9e10a241754096a46c28ee1f295f7`; runtime, UI correction, acceptance fixture, packaging and CI source are committed, including the factory RUNNING-state gate correction. This runtime-source checkpoint precedes the release-input commit that passed definitive full regression. |
| Exact release/source candidate | **`d20bf661a7fff42211dff0d62fe21c88746393e5`**; committed release inputs are locked. A pristine git archive passed definitive native-enabled full regression: **1049 passed, 24 deselected, 2 warnings, no failures or skips**, 629.44 seconds. |
| Built wheel | `foxprofile-2.4.0-py3-none-any.whl`, 902297 bytes; SHA-256 `c6992ffce1a96321ed681252d9dcb229acda73322ac8c0d9cdc490faf382a325`. Built from the exact d20bf66 candidate archive; isolated installed proof passed below. |
| Built sdist | `foxprofile-2.4.0.tar.gz`, 825800 bytes; SHA-256 `be4b6dc45ed86489987b1676a86e13d0be75ca9b8fcd5e05efd6a3de0060fb47`. Includes the complete wheel runtime payload. |
| Package/document hashes | Root [MANIFEST.sha256](../../../../MANIFEST.sha256) covers the OpenSpec delivery and deployment inputs. Regenerate after final integration; it is not a wheel digest or dependency lock. |
| Compatibility manifest | [foxprofile_ext/compatibility.json](../../../../foxprofile_ext/compatibility.json): schema version, finite upstream hook register, route/service/launch inventories, adapter contracts and focused evidence. Its immutable metadata identifies the runtime-source checkpoint separately from the tested release-input commit; release-owner completion records must preserve this distinction. |
| Database/catalog | SQLite schema **5**; additive sidecar, immutable local UUID-hex profile IDs, omitted from portable archives. Existing catalog/browser directories remain the source of profile data. Schema-only [schema-v5.json](../../../../deploy/extension/schema-v5.json) SHA-256: `3d078a14687be573088ff700d6234687e5243bc48548ee3f98e7693b01d1898b`. Generated from fresh disposable ExtensionDatabase sqlite_schema SQL only (19 statements), no user/settings/session rows. |
| Tested development/test dependencies | [dependencies-tested-py312-linux.txt](../../../../deploy/extension/dependencies-tested-py312-linux.txt), 92 `pip freeze --all` entries, including `tzdata==2026.6`, SHA-256 `56a45b7ce21c1a2cce32067d4b408e2ca394dd19e81f7bd675cd68877ed74127`. Observed Linux/Python 3.12.14 development/test/build environment; no private URLs/paths. Not a production-only lock or wheel/image digest. |
| Retained executable dependencies | All **92** matching Linux/Python 3.12 dependency wheels, **110686743 bytes**, are retained beside the release distributions in `dependencies/`; `dependencies/SHA256SUMS` SHA-256 **`4b860111d46f27222d33d45ef9b896b760978d1247d980f21e0b21b518c4330a`**. Fresh offline installation and `pip check` passed; every installed version matches the freeze. This wheel set does not establish ARM64 or native/browser availability. |
| Native confinement fixture | bubblewrap **0.12.0**, executable SHA-256 `573236e5328ac2ebb08f59ae3a9805b4f8d12bdef14be8af4450d5463294985f`. Development Linux x86-64 only; target identities remain preflight inputs. |
| noVNC | v1.7.0, commit `63107bd06d9e1f6136ff21aeda8cd62cbf0d433e`; original archive SHA-256 `06dac239be3973ed79a00e357a68aab996e6ed2fd496ee2cc6e6a34020344ac9`. Per-file digests/licenses: [provenance.json](../../../../foxprofile_ext/assets/novnc/provenance.json). |
| Native VNC fixture | Linux amd64 x11vnc package **0.9.17-1**, executable SHA-256 `249bdc17e09fd96ea73ca7fbec2d8f3cd9ff411d0bbfb634d99431548fb77132`. This digest identifies the tested local binary only; verify each host/architecture independently. ARM64 digest unknown. |
| Browser builds | Actual Camoufox/Chrome build identity **NOT RUN**; Python package versions below do not identify installed browser binaries. Deployment preflight must supply executable paths, builds and hashes. |

The immutable compatibility manifest records `e959a26e11c9e10a241754096a46c28ee1f295f7` as the runtime-source checkpoint; the exact packaged-release/source candidate is `d20bf661a7fff42211dff0d62fe21c88746393e5`. This handoff update and its regenerated document manifest belong to a later documentation-only revision, whose own hash cannot be embedded before commit. They do not change the tested release or its built artifacts. A separate deployment agent verified and accepted these immutable inputs; task 8.6 is complete while all five slice-09 tasks remain unchecked. Artifact binaries remain outside git and are delivered by their recorded filenames/digests, including the matching dependency wheels.

## Observed dependency versions

Read from the fresh development installation using `importlib.metadata`; the full 2026-11-07 development/test freeze is delivered above. The matching installable wheel set is retained and independently installed offline as recorded above; preserve it with the selected release. `requirements.txt` includes ranges; rollback must not resolve fresh dependencies.

| Runtime / tool | Version |
| --- | --- |
| Python | 3.12.14 |
| Declared FoxProfile distribution | 2.4.0; the fresh development environment runs the checkout, not an installed FoxProfile distribution. |
| camoufox / playwright | 0.5.9 / 1.62.0 |
| fastapi / starlette | 0.162.0 / 1.10.0 |
| flet / uvicorn / aiohttp | 1.1.1 / 0.54.0 / 3.14.5 |
| argon2-cffi / websockets | 25.1.0 / 17.4 |
| pytest / ruff | 9.2.0 / 0.17.4 |
| tzdata (development/test Python package) | 2026.6 |

Optional protected dependencies come from [requirements-extension.txt](../../../../requirements-extension.txt); the development install includes [requirements-dev.txt](../../../../requirements-dev.txt). Exact native Xvfb/xauth/bubblewrap versions and target package inventories must accompany the final deployment manifest; local compatibility metadata is not proof of a target's installed packages.

## Offline evidence already reported

Results below are checkpoint evidence supplied by implementation owners. They do not sum to a unique test count; the definitive integrated result appears below. Historical failures remain visible even where a later focused correction passed.

| Checkpoint / scope | Observed result | Limit |
| --- | --- | --- |
| Audited upstream baseline | 458 passed, 24 browser tests deselected; required tzdata in disposable environment. | Exact baseline scope and earlier foundation checks: [implementation-baseline.md](implementation-baseline.md). |
| `46d0a33783b9b9475ec5a670a2ac239fe8eeb67f` protected API/runtime | 79 passed on frozen index. Earlier broad offline run: 784 passed, 1 isolation failure, 16 skipped, 24 deselected; correction's hook checks: 34 passed, 1 skipped. | Historical broad run was not green; superseded by the exact-candidate full result below. |
| `0e6d1844260f80b70d89d975b91b0d9b3c703659` private VNC backend | Independent focused checks: 54 passed; native-X fixtures: 18 passed. | Synthetic X server/client fixtures, not real Camoufox or two-host acceptance. |
| `87947c6c2cd2aeb53f900b37400b83e12b0e740c` UI/API contracts | 51 passed plus 3 Origin checks passed. | Contract scope; actual UI review remains separate. |
| `07ce9873821102d4816091824d93bd550d1e8886` composed gateway | HTTP/service checks: 76 passed; native fixtures: 18 passed; composition: 4 passed; missing-native-config check passed. | No real target browser/proxy/load claim. |
| Compatibility/update gate | `tests/test_ext_compatibility_gate.py`: 26 passed. Related checks: 90 passed, 1 skipped before final gate tightening; final gate rerun passed. Disposable drift rejection, positive adaptation and installed-shape checks passed. | Finite hooks/inventory in compatibility manifest; no additional real upstream candidate selected (U04 N/A). |
| Committed Web/Flet canonical-ID correction | Fresh targeted proof: web 38 passed, desktop 36 passed, locator 11 passed; API/access 75 passed, 1 skipped. The mutable-name modal P2 correction is committed: `id:<UUID>` resolves IDs only, with no name fallback; extension profile_id fields remain strict raw UUIDs. | Independent final UI review passed six targeted Chromium regressions with zero skips (27.28 seconds, one existing warning), including actual auth/catalog/API rename/deletion with owned/foreign replacement UUID-like names, edit selection, batch/Viewer/manual open and stop. Source and tests remained unchanged. These regressions are included in the definitive full suite below; earlier counts are not cumulative. |
| Exact d20bf66 distributions and installed proof | Actual wheel and sdist built from a separate pristine git archive. Wheel installed with `--no-index --no-deps --no-compile --target` outside checkout/CWD; 225 wheel runtime files and 78 assets matched source byte-for-byte, and sdist contains the entire wheel runtime payload. Source hook and assembled protected route gates passed: 74 routes, 69 hooks, 38 launch callsites. | Package/import/resource/gate proof only; no real service, browser or live acceptance started. The definitive full-suite result is recorded below. |
| `ffc1502d44343d9653bb849c3e24b7b000cf1b21` maintenance | 113 passed; independent reviewer reran the same 113 with all passed. Earlier catalog temporary-file symlink truncation (P1) and lock symlink chmod (P2) findings are closed. | Inputs and service-owned parent directories must stay trusted and immutable during maintenance. An actively hostile parent-directory writer is outside this guarantee and could replace the catalog itself. These regressions are included in the definitive full suite below. |
| Fresh development install / CI categories | Final fresh-environment category run: 163 passed, 18 optional native checks skipped. Dedicated native command: 89 passed, zero skips, including Chromium UI and packaging. Dependency `pip check`, actionlint and Ruff passed. | Cross-OS and Python 3.10 execution remain NOT RUN; native-enabled Linux integration passed below. |
| nginx deployment template syntax | Actual delivered template passed `nginx -t` using compiled official nginx 1.28.2, GCC 14.2, OpenSSL 3.5.7; binary SHA-256 `f7579b4f791a5215b76bf55f0ac68cfe5709c5c686ee0a8ca1f1725605363dd0`. | Ephemeral events/http wrapper and self-signed example.test certificate; no daemon, real TLS endpoint or target configuration tested. |
| `26fcf9e6556cef5cd42242b2d8d3bee57e9c260e` native-enabled full-suite attempt | 978 passed, 3 timezone failures, 24 deselected; stopped by maxfail=3 after 613 seconds. The fresh development environment lacked tzdata; after installing tzdata 2026.6, the focused timezone/module check passed 47 tests. | This historical full run **FAILED**; it is not final acceptance. The refreshed dependency report includes tzdata; the later definitive full run passed below. |
| `e959a26e11c9e10a241754096a46c28ee1f295f7` RUNNING factory gate | Factory now requires an actual RUNNING state before native backend startup. Focused proof: HTTP 46 passed, 1 optional native skip; native RFB 1 passed; service/store 45 passed. | Targeted correction proof, also included in the later definitive full suite below. |
| Committed acceptance fixture | `python -m pytest -q tests/test_ext_acceptance_fixture.py`: final 15 passed, including canonical-prefix checks and real loopback HTTP smoke in a subprocess using the actual isolated Container/auth/catalog. | Offline fixture/smoke evidence only; no actual browser, native noVNC, target or source-case acceptance run. Acceptance module and controlled HTML are present and byte-verified in the actual installed wheel. |

The definitive full run used a pristine `git archive d20bf661a7fff42211dff0d62fe21c88746393e5` tree and the tested Python 3.12 environment with tzdata 2026.6. Native X tools and libraries plus the installed Playwright Chromium fixture were available. With local runner paths supplied through the aliases below, the command was:

```bash
PATH="$NATIVE_TOOLS:$PATH" FOXPROFILE_RUN_NATIVE_X_TESTS=1 \
FOXPROFILE_NATIVE_LIBRARY_PATH="$NATIVE_LIBRARIES" \
PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_FIXTURES" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
"$TEST_PYTHON" -m pytest -q -m 'not browser' --maxfail=3
```

**Result: 1049 passed, 24 browser tests deselected, 2 warnings, 629.44 seconds; zero failures and zero skips.** The externally retained full-run receipt (`verify-d20bf66.log`) has SHA-256 `6bab46d5db5cd4335c84acf98e168275c15a913655c220d43ab12d2344293493`; raw logs are excluded from git. The warnings were Starlette/httpx deprecation and the direct-fork multithreaded native-helper fixture. This supersedes the failed timezone-environment run without erasing its result. The native-X, confinement, RUNNING-state, web/Flet identity, maintenance and packaged-fixture regressions are included. Playwright Chromium here tests the panel/fixtures; it does not establish actual Camoufox/Chrome engine, target-host or source live acceptance.

## Exact-candidate artifact verification

The external delivery root `release-d20bf66/` retains `dist/` (wheel and sdist), `dependencies/` (92 wheels and `SHA256SUMS`), `installed-proof.json` and `dependency-evidence/{proof,versions}.json`. Preserve these together with the committed freeze, schema, configuration examples and this handoff; generated build/install trees are not deployment inputs.

A separate `git archive d20bf661a7fff42211dff0d62fe21c88746393e5` supplied the release build tree, independent of the regression tree. The tested Python environment invoked `setuptools.build_meta.build_sdist(output_dir)` and `setuptools.build_meta.build_wheel(output_dir)` there with `SOURCE_DATE_EPOCH=1794004780` (candidate commit timestamp) and `PIP_NO_INDEX=1`. No additional build dependency was downloaded. The environment variable was supplied for reproducible timestamps; no unsupported claim of byte-identical repeated sdist builds is made.

The actual wheel was installed from outside the checkout:

```bash
python -m pip install --no-index --no-deps --no-compile --target INSTALLED_TARGET foxprofile-2.4.0-py3-none-any.whl
```

An isolated `python -I` process with an external CWD imported `src`, `chrome_engine`, `foxprofile_ext`, `foxprofile_ext.acceptance`, `foxprofile_ext.manual.factory` and `foxprofile_mcp` exclusively from that target. It executed `require_compatible_hooks(root=installed_target)` and `check_compatibility(root=installed_target, app=create_app(object()))`, both passing. The object container assembled the protected routes without starting real services or browsers.

Every wheel runtime file matched its installed bytes; installed assets also matched the exact archived source. This includes the controlled acceptance HTML, all three new panel assets, five TTF fonts with two OFL license files, and all **61 noVNC files listed by provenance plus provenance.json itself (62 files)**. Every noVNC provenance digest matched. The sdist includes the complete wheel runtime payload. Exact artifact digests are listed above; the external `installed-proof.json` receipt records module paths and counts (SHA-256 `cfdfb16a4b68ae7ff3ee7b7e0c93ec558a08b8632094defebb833db78edacbb6`).

The adjacent dependency receipt (`dependency-evidence/proof.json`, with `versions.json`) records all 92 wheel pins matching the lock, `offline_install_exit: 0`, `pip_check_exit: 0`, and `versions_match: true`. Reproduce the dependency installation without index resolution using the retained directory and lock:

```bash
sha256sum -c SHA256SUMS
python -m pip install --no-index --find-links . -r "$TESTED_DEPENDENCY_LOCK"
python -m pip check
```

Run these commands in the retained `dependencies/` directory, using a fresh Python environment and the absolute path to the delivered freeze as `TESTED_DEPENDENCY_LOCK`. The freeze includes development/test/build tools, not only production dependencies. Native tools and browser binaries require separately verified target packages/builds, especially on ARM64.

## Deployment inputs and operating commands

Use [docs/EXTENSION_DEPLOY.md](../../../../docs/EXTENSION_DEPLOY.md) ([Vietnamese](../../../../docs/EXTENSION_DEPLOY.vi.md)) as the operational procedure. Delivered templates are in [deploy/extension](../../../../deploy/extension/): `target.json.example`, `installation.json.example`, `runtime.env.example`, `secrets.env.example`, `foxprofile-extension.service.example`, and `nginx.conf.example`. They are templates, not discovered live service definitions. Host paths, users, access, installed revisions, architecture, namespace, TLS and resource policy remain unresolved for the deployment agent.

Use exactly **one lifecycle manager and one ASGI worker per installation**. Browser, Xvfb, Xauthority and temporary VNC helper share that service/container's private temporary namespace. Shared SQLite admission does not provide cross-worker gateway/backend IPC. Do not use multiple uvicorn/gunicorn workers or run a second desktop manager over the same dataset. Preserve a saved valid capacity; absent capacity defaults to 10. Chrome keeps ordinary authorized management and shared admission; manual noVNC is explicitly unsupported.

Set the private environment before Python imports `src.core.config`: `FOXPROFILE_EXT_ENABLED=true`, `FOXPROFILE_HEADLESS=virtual`, explicit database/catalog/profile/log paths, installed engine paths, independently verified `FOXPROFILE_X11VNC_EXECUTABLE`/`FOXPROFILE_X11VNC_SHA256`, loopback bind and same-origin HTTPS/WSS proxy. Keep the service API credential in the service-owned private secret file (0600 in a 0700 directory) or existing secret manager. No credentials in arguments, URLs, reports or A/B's browser. Personal users receive personal sessions. Missing confinement/runtime/hooks must fail protected startup or manual access; never disable protected mode as recovery.

The packaged operational commands are:

```bash
python -m foxprofile_ext.maintenance --help
python -m foxprofile_ext.maintenance backup --installation /private/operator/installation.json --stopped-and-closed --destination /private/backups/before-change
python -m foxprofile_ext.maintenance restore --snapshot /private/backups/before-change --empty-disposable-destination /private/staging
python -m foxprofile_ext.maintenance migrate --installation /private/staging/installation.json --stopped-and-closed
python -m foxprofile_ext.bootstrap --database /private/staging/state.sqlite3 --username admin0
```

These are operator-resolved placeholder paths. First fence admission, close browser/helper/display runs normally, stop the owning manager and verify process absence; `--stopped-and-closed` is an explicit operator attestation. Restore requires an existing empty disposable destination. Rerun migration on the copy to verify idempotency. Do not source the restored original configuration unchanged in staging: it may still point to production. See the deployment guide for descriptor layout, snapshot confidentiality, shutdown checks, permission limits, staging isolation and matched rollback. Restore code/configuration/catalog/sidecar/profile data and pinned executable dependencies together; no guessed schema downgrade or old unprotected public fallback.

The delivered [acceptance fixture guide](../../../../docs/EXTENSION_ACCEPTANCE.md) ([Vietnamese](../../../../docs/EXTENSION_ACCEPTANCE.vi.md)) documents the packaged runner and controlled input page:

```bash
python -m foxprofile_ext.acceptance prepare --destination /tmp/foxprofile-acceptance --credentials-file /tmp/acceptance-credentials.json
python -m foxprofile_ext.acceptance serve-page --port 8765
python -m foxprofile_ext.acceptance smoke --fixture /tmp/foxprofile-acceptance --expect-fixture FIXTURE_UUID_FROM_LOCAL_MANIFEST --base-url http://127.0.0.1:8800 --credentials-file /tmp/acceptance-credentials.json
```

Use a private, operator-owned 0600 credentials file with `admin0_password`, `a_password`, `b_password` and `service_token` (at least 24 ASCII characters), or omit it for masked terminal prompts. Preparation refuses nonempty or symlinked storage. Keep generated `runtime-env.json` private: it contains the service token for server configuration. The page binds loopback only. Smoke requires the explicit fixture ID and target origin, validates the exact disposable catalog/user identities, and changes only its login/logout session rows. Read the guide and complete deployment preflight before starting a staging server. Passing these narrow smoke groups does not mark A/N/S/X live cases passed.

Update rehearsal uses the existing checker; no new inventory or hook register is introduced:

```bash
python tools/check_extension_compatibility.py
python tools/check_extension_compatibility.py --rehearse
python -m pytest -q tests/test_ext_compatibility_gate.py
```

Rehearsal uses a disposable copy and rejects unclassified routes/launches, missing admission calls and signature drift, then verifies the restored candidate. A real upstream candidate is conditional U04; synthetic drift and existing upstream regression gates remain mandatory.

## Separate-agent receipt and live boundary

On **2026-11-07**, a separate prospective deployment agent (IND901) independently verified the exact d20bf66 wheel/sdist and installed-proof digests, all 92 dependency wheels and their manifest, the dependency lock, schema and native fixture digests, definitive 1049-test result, six-case independent UI closure and 113-test maintenance closure. It accepted the offline handoff for slices **1–8**, including task **8.6**, with no remaining implementation blocker. This receipt completes **40/45 tasks**; all five slice-09 tasks remain unchecked and target preflight is **BLOCKED** until actual host/access, native/browser pins, TLS, namespaces and resource constraints are resolved. The delivered fixture contains `admin0`, A/B, separately trusted service access, 11 Camoufox/Linux profiles with stable IDs and capacity 10, plus the controlled input page. Its preparation and smoke checks run only against explicitly selected disposable staging storage; no real server has been operated. Track remaining work in the repository [TODO](../../../TODO.md) through its release owner; do not turn future work into unrecorded prose-only deferrals.

The separate deployment agent must read the complete [slice-09 runbook](../slices/09-live-deployment-and-acceptance.md) and [DEPLOYMENT_AGENT_PROMPT.md](../../../../DEPLOYMENT_AGENT_PROMPT.md), verify final immutable inputs, resolve actual host/access/topology and native/browser versions, take coherent private backups and rehearse restore/migration on isolated copies, then run the complete acceptance inventory. Required live evidence includes all **A01–A30**, **N01–N13**, **S01–S03**, relevant **X** boundaries, update gates and both-host **1/5/10** browser measurements before/during/after manual access.

Actual Camoufox/Chrome execution, proxy/WebRTC egress, persisted real login/fingerprint, browser/display/page geometry, native-dialog confinement on the target service UID/namespaces, ARM64 behavior and resource capacity remain **NOT RUN**. No hardware/access details have been supplied; both hosts are **BLOCKED for deployment preflight**, not accepted. Record unavailable mandatory environments as BLOCKED and preserve unexecuted live cells as NOT RUN. Only the separate deployment agent may populate the final deployment report and complete task group 9. No archive before mandatory live evidence and unresolved failures close.
