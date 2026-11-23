# Distributed browser workers — xhigh review and fixes

Date: 2026-11-23. Branch: `codex/openspec-multi-user-manual-browser-workflow`.

## Scope and outcome

The frozen review scope is `cea6a094659edc7e30b1707dd770a2e47c2463cf..ed68c3b69c745f27f25109e966a94901c6b9c7f2`: 70 files and 14,685 changed lines after scope filtering. Review effort: `code-review-cc -xhigh -fix`. Candidate verification ran independently in batches as requested.

Eighteen angle/shard finders completed the first pass. A fresh reviewer then completed the single additional gap sweep required at xhigh. The final deduplicated audit has **75 candidates: 74 confirmed, 1 plausible, 0 refuted**. Outcomes: **74 fixed, 1 skipped, 0 required no change**. Thirteen independent verification batches and scoped fix tests precede the integrated checks below. Overlapping targeted test counts are not summed.

The [ranked report](code-review-cc.json) contains the 15 highest-priority surviving findings. The [complete audit](code-review-cc-audit.json) preserves every deduplicated candidate, its independent verdict and final disposition. Finding file/line locations refer to the frozen review head, so they remain meaningful after fixes move code.

## Behavior covered by the fixes

- Exact run, Worker boot and connection ownership survive reconnects, watchdog cleanup, cancellation and delayed replies. A timeout never substitutes for proof of process-tree death.
- Worker admission persists its exact owner atomically. Missing or slow telemetry blocks placement while authenticated control stays live; cgroup uncertainty fails admission closed, and each node supplies its own memory reserve. Heartbeat and loss deadlines are validated together.
- Canonical data preserves LevelDB logs, validates anchored source files, uses bounded transfer staging and retains clean stopped data for retry. Durable content evidence supports crash recovery and avoids verification ZIPs. Local natural exit, Stop and shutdown use one trusted completion path.
- Manual access checks current runtime support and confinement before taking the shared lease. Relay setup, failure, revocation and cancellation close their owned resources.
- Expensive transfer and relay checks leave event loops and catalog operations responsive. Completed run objects are released, recovery skips already-finished receipts, and independent batch Stop calls overlap within a bounded pool.
- Desktop and web retain metadata editing semantics, exact Stop intent, localized Worker status, distinct missing telemetry, and a two-line status layout.
- Current runtime, dependency and distribution evidence are separated from historical single-host releases, with source-specific artifact attribution in the compatibility register.

## Integrated verification

| Check | Result |
| --- | --- |
| `python -m pytest -q -rs -m 'not browser' --maxfail=10` | 1375 passed, 64 skipped, 24 deselected, 11 warnings in 279.27s (0:04:39); log SHA-256 996536ce5d6c535c141d4b1e22453b021f472706110999ddd9d6b5477f12ec51 |
| `ruff check src tests chrome_engine foxprofile_ext tools` | PASS; the subsequently corrected test fixture also passed its scoped lint check. |
| `ruff format --check src tests chrome_engine foxprofile_ext tools` | PASS: 257 files formatted; the subsequently corrected fixture also passed its scoped format check. |
| `python tools/check_extension_compatibility.py` | PASS: 84 route/method entries, 94 hooks, 42 launch callsites. |
| `python tools/check_extension_compatibility.py --rehearse` | PASS: unclassified route, missing admission hook, adapter signature drift and new browser factory rejected; restored disposable candidate passes. Log SHA-256 8f9f15e8aec671d1f3310762e43eb0807cc1f3bada893f169ee2b2715e0e2ca8 |
| `node --check (panel.js and extracted web inline script)` | PASS: both JavaScript surfaces parse. |
| `git diff --check` | PASS. |

This source checkpoint contains the completed runtime fixes and integrated checks. The exact source commit, matching wheel/sdist digests and final metadata/packaging check will be recorded by the following evidence commit; historical release artifacts do not describe this runtime.

The real localhost TLS checks include normal authenticated Worker commands and full data-return/receipt flow with a synthetic runtime, plus rejection of control and RFB redirects before a second origin is contacted. They do not launch a native browser.

## Explicit limits and deferred work

CC19 remains plausible and skipped: the selected deployment design entrusts host-specific native evidence to external provisioning. Adding application host attestation would introduce a new provenance policy. The exact-host replacement/restore check remains in `openspec/TODO.md`.

Native Chromium/Camoufox, Xvfb/x11vnc, browser/Flutter pixel rendering, non-Linux executors and deployed two-host failure acceptance were not established by this review. Browser-backed UI checks remain skipped where their executable or sandbox prerequisites are unavailable. DOM doubles, constructed Flet controls, bundled-font estimates, real temporary SQLite databases and harmless owned process fixtures are bounded evidence for the corresponding paths.

Pre-change interrupted journals without verified content bindings are preserved for offline recovery. A legacy revision without a wire/content association can conservatively advance once on its first direct local completion. A clean stopped Worker copy retained after staging exhaustion cannot be automatically adopted by a replacement connection. These recovery obligations and the existing native, resource, partition and backup/restore gates are retained in the repository TODO. No production host or service was changed, and the OpenSpec change remains unarchived.

## Review and publication controls

Code Simplifier was applied to the completed source fixes and to publication metadata. Compatibility hooks were updated only for reviewed behavioral changes and tested together with rejection of unsafe synthetic drift. The source publication uses a matching Git tree, noreply commit identities, the observed branch head and a non-forced compare-and-swap ref update. The user authorized push to this existing branch.
