"""Small cross-process lock for local catalog mutations."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held_locks = threading.local()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def catalog_lock(path: str | Path) -> Iterator[None]:
    """Serialize catalog changes between threads and local processes."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    thread_lock = _thread_lock(lock_path)
    key = str(lock_path.resolve())
    with thread_lock:
        held = getattr(_held_locks, "entries", None)
        if held is None:
            held = _held_locks.entries = {}
        if key in held:
            held[key][1] += 1
            try:
                yield
            finally:
                held[key][1] -= 1
            return

        with lock_path.open("a+b") as lock_file:
            if os.name == "posix":
                os.chmod(lock_path, 0o600)
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.seek(0)
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            held[key] = [lock_file, 1]
            try:
                yield
            finally:
                del held[key]
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
