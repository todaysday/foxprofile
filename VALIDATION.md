# Package validation

**Updated:** 2026-11-07. **Change:** `add-multi-user-manual-browser-workflow`.

Specification structure passed strict OpenSpec validation during authoring. Implementation and offline release checks passed: exact candidate d20bf66 produced 1049 passed, 24 deselected and two warnings, with zero skips/failures in the native-enabled full suite. Distributions, installed-artifact compatibility and fresh offline installation of all 92 retained dependency wheels passed. Maintenance P1/P2 review findings are closed and the disposable runtime fixture is delivered. Independent UI review is closed and a separate deployment agent has verified and accepted the offline handoff. The canonical product evidence and handoff is [implementation-handoff.md](openspec/changes/add-multi-user-manual-browser-workflow/references/implementation-handoff.md). Live deployment/acceptance remains **NOT RUN**, owned by the separate slice-09 deployment agent. Document checks do not establish product or host acceptance.

## OpenSpec validation

The official `@fission-ai/openspec` CLI **1.16.0** is installed outside the delivered package. Run from the repository root with telemetry disabled:

```bash
OPENSPEC_TELEMETRY=0 openspec validate add-multi-user-manual-browser-workflow --type change --strict --no-interactive --json
OPENSPEC_TELEMETRY=0 openspec status --change add-multi-user-manual-browser-workflow --json
OPENSPEC_TELEMETRY=0 openspec instructions apply --change add-multi-user-manual-browser-workflow --json
```

Latest implementation-document validation on **2026-11-07** exited **0** with `valid: true` and **zero issues**; status confirmed all four required planning artifacts present. The CLI status field `isComplete` describes planning artifacts, not completed implementation or live acceptance. Planning completeness is distinct from task completion. Exact product test results, historical failures, known limitations and remaining release gates are recorded only in the canonical handoff.

## Structural and source coverage

| Capability | Requirements | Scenarios |
| --- | ---: | ---: |
| `global-profile-capacity` | 12 | 24 |
| `manual-browser-access` | 14 | 36 |
| `persistent-stealth` | 7 | 14 |
| `profile-authorization` | 14 | 38 |
| `upstream-compatibility` | 9 | 14 |
| `user-authentication` | 9 | 22 |
| `virtual-browser-runtime` | 8 | 14 |
| **Total** | **73** | **162** |

The seven capability specs retain 73 stable requirement IDs and source traceability. The 45 tasks are grouped into nine slices: 40 implementation/offline tasks in slices 1–8 and five separate deployment tasks in slice 9. OpenSpec apply inspection confirms **40/45 tasks complete**, with all of slices 1–8 checked and all five slice-09 tasks unchecked. Checked tasks mean implementation/offline verification and the accepted handoff, not live acceptance. The acceptance plan retains all **A01–A30**, **N01–N13**, **S01–S03**, focused X cases and both-host resource cells as NOT RUN. U04 is conditional N/A until an additional real upstream candidate is selected; baseline and synthetic drift checks remain mandatory.

The audited upstream is commit `530b993ff3a7d211aaa0904fba0817b77c75d4c9`, declared FoxProfile version 2.4.0; actual installed target revisions remain unknown. Source provenance, licenses and adaptation rationale are in [upstream-audit.md](openspec/changes/add-multi-user-manual-browser-workflow/references/upstream-audit.md) and [reuse.md](openspec/changes/add-multi-user-manual-browser-workflow/reuse.md). CLI validation does not verify these claims or replace independent fidelity/simplicity review recorded by the release owner in `completion.review`.

## Immutable original inputs

The three bundled originals remain byte-for-byte unchanged:

| File | SHA-256 |
| --- | --- |
| `FoxProfile_Stealth_Configuration.md` | `3e6798a88b30ff2cde661130fb04c126e9dd193e00044312e4e3e5ae36af4f16` |
| `FoxProfile_User_Access_Spec.md` | `7d66bbe8693f30f9fef5088364516573618e102cb8ef61708d283d67ef1df14f` |
| `FoxProfile_noVNC_Manual_Handoff.md` | `59a3904023c11b0d8b6daa36f543fe319909ba76b5ac9ada6cad86d6eca9cc73` |

## Document and deployment-input integrity

Root `MANIFEST.sha256` lists **51 files** and covers the OpenSpec delivery documents, original inputs, canonical handoff and listed deployment/configuration/packaging inputs. Its scope is the paths it lists, excluding itself. It is neither a complete repository/source inventory nor a hash of the final built distributions or dependency environment. The canonical handoff records the exact tested d20bf66 release, wheel/sdist/dependency/schema digests and external installed-artifact receipts. A later documentation-only completion commit does not change that tested release identity. No new ZIP archive is claimed.

Verify listed files from the repository root, and regenerate their digests after any subsequent release-owner edits:

```bash
sha256sum -c MANIFEST.sha256
```

No real host, service configuration, credentials, browser data, proxy/session details or operational backups are included. Required live Camoufox/Chrome, native-isolation, geometry, proxy/WebRTC and ARM64/x86-64 load evidence remains NOT RUN.
