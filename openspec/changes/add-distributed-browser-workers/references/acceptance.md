# Acceptance matrix — distributed browser workers

Every test records baseline revision, environment/runtime manifest digest, profile revision before/after, exact run/worker identifiers (non-secret), observed results and whether it was mocked, native-isolated or on a real two-host deployment. No live test is represented as already run.

| Case | Setup / trigger | Required result |
| --- | --- | --- |
| DW-01 | Ubuntu 24.04 Worker connects with correct identity | Authenticated ready, fresh telemetry and manifest admitted |
| DW-02 | Expired, revoked, forged or reused Worker credential | Registration denied; no profile bytes or actions sent |
| DW-03 | Worker reports mismatched browser/font/Xvfb/rendering manifest | Incompatible, not eligible |
| DW-04 | Master has RAM headroom | Local launch keeps existing behavior |
| DW-05 | Master low RAM and two eligible Workers | Deterministic healthy Worker selected by headroom |
| DW-06 | Master and Workers have no safe headroom | Admission rejected; no partial profile copied or child started |
| DW-07 | Two concurrent launch requests same profile across separate Master API threads | Exactly one reserved run and at most one browser process tree |
| DW-08 | Two profile batch competes for last global slot or last Worker memory | Atomic batch rejection; no partial new starts |
| DW-09 | Export/cookie helper while remote run owns profile | Same exclusivity blocks invalid data access and second launch |
| DW-10 | Valid stopped profile starts remotely | Fingerprint/persona bytes identical; tabs/login/cookies restored |
| DW-11 | Partial snapshot, archive path injection, hash mismatch, disk shortfall | Worker rejects before browser start; canonical Master copy unchanged |
| DW-12 | Clean remote browser stop, complete upload | Master advances revision exactly once; cookies/storage/tabs persist |
| DW-13 | Upload ack lost, exact same upload retried | Same receipt, no second revision |
| DW-14 | Old run uploads after a new generation, wrong Worker or wrong base revision | Never overwrites canonical data; report stale/conflict |
| DW-15 | Remote Playwright navigate/click/screenshot from UI/MCP | Same Master route, authorized exact run, bounded response |
| DW-16 | Unauthorized user connects to remote control or manual stream | Access denied before any pixels or input |
| DW-17 | Remote Camoufox manual input, Back and disconnect | Same browser run; helper released; ordinary input restored; global capacity unchanged |
| DW-18 | Master–Worker link intentionally severed | Worker watchdog stops all process trees; Master retains lock until verified; canonical data intact |
| DW-19 | Worker kills only its main agent, child browser remains | systemd/supervisor forces child cleanup; cannot outlive loss budget as a controllable session |
| DW-20 | Master restart with live remote claim | No new placement until recovery; stale reports cannot free new run |
| DW-21 | Disconnected Worker with no death proof | Profile stays blocked; no second browser on any host |
| DW-22 | Trusted verified fence/power-off then recovery | Unlock exact old run, permit new launch without accepting old upload |
| DW-23 | Browser without configured proxy moves between VPS | No promised same external IP; API shows actual run placement |
| DW-24 | Worker drain during active session | No new placements; active run may cleanly stop and sync |
| DW-25 | Snapshot with metadata edited on Master after launch | Worker commit changes only browser-data revision, does not revert Master profile metadata |
| DW-26 | Two CPU architectures with standardized Ubuntu 24.04 deployment | Each preflight independently proves browser-visible geometry, WebGL/backend, font and identity equivalence; incompatible excluded |

Initial verification must cover pure DAO/policy unit tests, protocol test doubles, multi-process SQLite admission, controlled isolated browser launch, and a two-host network/kill matrix. Separately execute old extension regressions covering authentication, process confinement, global capacity, manual lease and transfer. Existing live slice-09 remains a distinct prerequisite.
