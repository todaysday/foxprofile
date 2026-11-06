"""Private namespace handshake before executing a browser-creating Python tool."""

import os
import sys


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        return 2
    print("BOUNDARY:" + os.readlink("/proc/self/ns/pid"), flush=True)
    # Avoid buffered reads: helper JSON following GO must survive exec intact.
    if b"".join(os.read(0, 1) for _ in range(3)) != b"GO\n":
        return 3
    os.execv(sys.executable, [sys.executable, *sys.argv[2:]])
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
