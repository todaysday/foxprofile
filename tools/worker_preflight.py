#!/usr/bin/env python3
"""Generate or verify a measured Ubuntu 24.04 Worker runtime manifest."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from foxprofile_ext.workers.manifest import (
        ManifestError,
        generate_runtime_manifest,
        load_manifest,
        write_runtime_pin_measurement,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--native-evidence", type=Path)
    parser.add_argument(
        "--expected-sha256", default=os.getenv("FOXPROFILE_WORKER_EXPECTED_MANIFEST_SHA256")
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--measure-pins",
        type=Path,
        metavar="OUTPUT",
        help="write measured binary/package/font pins and digest without native PASS assertions",
    )
    modes.add_argument(
        "--generate",
        action="store_true",
        help="measure this host and add its architecture entry to the manifest",
    )
    args = parser.parse_args()
    try:
        if args.measure_pins:
            digest = write_runtime_pin_measurement(args.measure_pins)
            print(f"measurement_status=MEASURED_ONLY runtime_pins_sha256={digest}")
            return 0
        if args.manifest is None or args.native_evidence is None:
            parser.error(
                "--manifest and --native-evidence are required for generation or verification"
            )
        if args.generate:
            digest = generate_runtime_manifest(args.native_evidence, args.manifest)
            print(f"manifest_sha256={digest}")
            return 0
        if not args.expected_sha256:
            parser.error(
                "--expected-sha256 or FOXPROFILE_WORKER_EXPECTED_MANIFEST_SHA256 is required"
            )
        result = load_manifest(args.manifest, args.native_evidence, args.expected_sha256)
        print(
            f"preflight=PASS manifest_sha256={result.digest} ready_engines={','.join(result.ready_engines)}"
        )
        return 0
    except ManifestError as error:
        print(f"preflight=FAIL reason={error}", file=sys.stderr)
        return 1
    except OSError:
        print("preflight=FAIL reason=runtime evidence is unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
