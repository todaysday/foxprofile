# Upstream 2.4.1 integration

Date: 2026-11-23. Target branch: `codex/openspec-multi-user-manual-browser-workflow`.

## Inputs and merge

- Previous fork: `d62dd92cfdc073d78ddcec1c13342f88c69861dc`.
- Incorporated upstream: `chienbm98/foxprofile:main`, `8b5c49dca4b18c13cb6989654daa86e849459bc9`, release 2.4.1.
- Shared upstream baseline: `530b993ff3a7d211aaa0904fba0817b77c75d4c9`.
- Seven upstream commits change seven files: timezone alias diagnostics and its regression, dependency floors, API/package version, and changelog.

The candidate was prepared in an isolated worktree. Changelog resolution retains all fork Unreleased entries before the incoming 2.4.1 release. Development requirements retain extension and packaging dependencies, adopt upstream pytest/Ruff floors, and align the cross-platform test tzdata floor with 2026.6. No extension runtime behavior or hook contract changed.

The hook register retains its original audited baseline; `merged_upstream` records the newly incorporated revision. Previous 2.4.0 review and artifact receipts are historical. The separate `src`, `chrome_engine` and `foxprofile_ext` packages and their packaging entries are preserved.

## Verification

| Check | Result |
| --- | --- |
| `python -m pytest -q -rs -m 'not browser' --maxfail=5` | 1376 passed, 64 skipped, 24 deselected, 11 warnings in 279.51s (0:04:39); log SHA-256 `e60907bac8188cf4d036a3b4f7aae543396513c6405961dfb8312f0715d5cef8` |
| `python -m pytest -q chrome_engine/tests/test_probe_check.py tests/test_ext_compatibility_gate.py` | 81 passed, 2 warnings; log SHA-256 `660156a47b64fac63ded0805f5d19fc22ec771df2240aa4a90ea306dcd1427f4` |
| `python tools/check_extension_compatibility.py` | PASS: 84 route/method entries, 94 hooks, 42 launch callsites; log SHA-256 `23584c2be4352404bf642e669cff5d5154dec9c359755e34787c853b46637dc0` |
| `python tools/check_extension_compatibility.py --rehearse` | PASS: all four unsafe drift cases rejected; restored disposable candidate passes; log SHA-256 `8f9f15e8aec671d1f3310762e43eb0807cc1f3bada893f169ee2b2715e0e2ca8` |
| `ruff check and ruff format --check src tests chrome_engine foxprofile_ext tools` | PASS; 257 files formatted |
| `Package/API version and changed requirement floors` | PASS: both versions are 2.4.1; installed Camoufox 0.5.9, Flet 1.1.3, tzdata 2026.6, pytest 9.2.0 and Ruff 0.17.6 satisfy upstream floors |
| `git diff --check` | PASS |

The full run is the definitive test count; overlapping focused runs are not summed. Independent read-only review compared the incoming AST changes, checked all 15 timezone alias targets and verified installed dependency versions. Code Simplifier found no refactor warranted. No extension contract or behavior change was necessary.

Final locked-metadata and distribution selection: **39 passed, 3 warnings in 41.56s**; log SHA-256 `18d872b4b8a5f27b39e2bc7c8911007bd1f3e8ff240d8d8dd4d6d91c7384aec0`. This gate also verifies the sdist rebuild and isolated installed resources.

## Locked source and distributions

Source merge: [`3fc7d3e06be2cff59b229d348fe42920eae39c09`](https://github.com/todaysday/foxprofile/commit/3fc7d3e06be2cff59b229d348fe42920eae39c09), tree `0b5be382e7789e9f59ed614cc692bf2f4f9008a3`. Its first parent is the prior fork head and its second parent is the incorporated upstream commit; ancestry is preserved.

Built from a fresh Git archive with `SOURCE_DATE_EPOCH=1795433125`. All 239 runtime files match the source in wheel, sdist and isolated installation, with artifact allowlists checked.

| Artifact | SHA-256 |
| --- | --- |
| `foxprofile-2.4.1-py3-none-any.whl` | `a027fe4d82b8cf5138f3194aae7a17cc30693af41eafebaec26d6fef2e8bffca` |
| `foxprofile-2.4.1.tar.gz` | `80c8bbc8da24cccf3f065a3ee14d910e2af5f1dcf7994d10108e3189602d834d` |

The evidence-only commit following this source checkpoint records the hashes and final packaging check. It changes packaged `compatibility.json`, so these hashes identify the stated source artifacts; later branch-tip rebuilds are separate. Executable code and tests are unchanged by the evidence commit.

## Limits

Native browser/display/confinement prerequisites, non-Linux executors and deployed two-host acceptance remain unverified in this environment. This merge does not deploy a host, alter live data, or archive the existing OpenSpec changes.
