# Package validation

**Date:** 2026-11-05  
**Change:** `add-multi-user-manual-browser-workflow`  
**Result:** specification package passed validation. Product implementation and live acceptance have not been performed.

## Official OpenSpec validation

Validated with the official `@fission-ai/openspec` CLI **1.16.0**, installed locally for this authoring task.

```bash
export OPENSPEC_TELEMETRY=0
openspec validate add-multi-user-manual-browser-workflow --type change --strict --no-interactive --json
openspec status --change add-multi-user-manual-browser-workflow --json
openspec instructions apply --change add-multi-user-manual-browser-workflow --json
```

Observed results:

- Strict validation exit code: **0**.
- CLI version: **1.16.0**; telemetry was disabled with `OPENSPEC_TELEMETRY=0` for validation.
- Change `valid`: **true**.
- Validation issues: **0**; stderr empty.
- All four planning artifacts present: proposal, design, specs, tasks.
- Code-level provenance and adaptation review is recorded separately in `reuse.md`; CLI validation does not verify that evidence.
- Implementation state: **ready**, **0 of 45 tasks completed**, **45 remaining**.
- Planning completion means the specification artifacts exist; it does not mean the feature was implemented or accepted.

## Structural and coverage checks

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

Additional checks passed:

- All seven declared capability directories are present.
- Every requirement has a unique stable ID, SHALL statement, and concrete WHEN/THEN scenario; descriptions satisfy the schema's length guidance.
- No unknown requirement IDs, unresolved authoring markers, or broken relative Markdown file links were found.
- **45 unchecked tasks** are grouped into nine slices: **40 implementation/offline tasks in slices 1–8**, and **5 final deployment tasks in slice 9**.
- The original acceptance tables are covered exactly: **A01–A30**, **N01–N13**, **S01–S03**. Additional focused cases cover integration and upstream-update risks.
- All 73 requirement IDs are represented in the traceability artifact.
- All three source documents were copied **byte-for-byte unchanged**.
- Deployment/live acceptance is assigned to a separate final agent throughout config, proposal, design, tasks, prompts, acceptance plan, and the independent slice-9 runbook.

Mandatory product checks remain NOT RUN. A real-version upstream update rehearsal is conditional and N/A for initial release until an actual additional candidate is selected; baseline and synthetic compatibility tests are mandatory implementation work.

## Source integrity

SHA-256 of the included original inputs:

- `FoxProfile_Stealth_Configuration.md`: `3e6798a88b30ff2cde661130fb04c126e9dd193e00044312e4e3e5ae36af4f16`
- `FoxProfile_User_Access_Spec.md`: `7d66bbe8693f30f9fef5088364516573618e102cb8ef61708d283d67ef1df14f`
- `FoxProfile_noVNC_Manual_Handoff.md`: `59a3904023c11b0d8b6daa36f543fe319909ba76b5ac9ada6cad86d6eca9cc73`

## Source audit scope

The actual repository was checked out and inspected at commit `530b993ff3a7d211aaa0904fba0817b77c75d4c9` (declared FoxProfile version 2.4.0); review checkout `64b3cabfe741bf2d2955849aba4b03a50d94a6f7` adds documentation only. Pinned source links and observed integration details are in [upstream-audit.md](openspec/changes/add-multi-user-manual-browser-workflow/references/upstream-audit.md) and [reuse.md](openspec/changes/add-multi-user-manual-browser-workflow/reuse.md).

The full supplied source-grounded OpenSpec skill archive was installed; its `SKILL.md` matches the earlier supplied document byte-for-byte. The post-fix independent Stage 5 source-fidelity review and Stage 6 simplicity/reuse review are recorded in `completion.review`. CLI validation does not establish reuse provenance or replace those reviews. Stage 6 inspected existing FoxProfile source and cited tests; those tests were not run. Product implementation and live acceptance remain `NOT RUN`.

This was a source audit, not a running-browser audit. The production revision and host configuration have not been asserted or inspected. No fork feature was implemented, no real server was deployed or migrated, and no real browser/stealth/performance/acceptance result is claimed.

## Archive integrity

`MANIFEST.sha256` records every delivered file other than itself. The packaging step checks ZIP CRC integrity and verifies each extracted file against that manifest. The archive contains only this OpenSpec package and its included sources, not the upstream repository, temporary research checkout, installed CLI, credentials, or browser data.

From the extracted `foxprofile-openspec` directory on systems providing the standard tool, contents can be checked with:

```bash
sha256sum -c MANIFEST.sha256
```
