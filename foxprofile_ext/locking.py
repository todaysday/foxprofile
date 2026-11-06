"""Small cross-process lock for local catalog mutations."""

import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held_locks = threading.local()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _guard:
        return _thread_locks.setdefault(key, threading.RLock())


def _validate_lock_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("catalog lock must be a single-link regular file")


@contextmanager
def _open_lock_file(path: Path) -> Iterator[BinaryIO]:
    # O_NONBLOCK also prevents a FIFO swapped in after the path check from blocking open.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        _validate_lock_file(info)
        # Verify the opened object even on platforms without O_NOFOLLOW.
        current = path.lstat()
        _validate_lock_file(current)
        if not os.path.samestat(info, current):
            raise OSError("catalog lock changed during open")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b", closefd=False) as lock_file:
            yield lock_file
    finally:
        os.close(descriptor)


@contextmanager
def catalog_lock(path: str | Path, *, timeout: float | None = None) -> Iterator[None]:
    """Serialize catalog changes between threads and local processes."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        pass
    else:
        _validate_lock_file(info)
    thread_lock = _thread_lock(lock_path)
    key = str(lock_path.resolve())
    if timeout is not None and (timeout < 0 or not timeout < float("inf")):
        raise ValueError("lock timeout must be finite and non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout
    if not thread_lock.acquire(timeout=-1 if timeout is None else timeout):
        raise TimeoutError("catalog lock is busy")
    try:
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

        with _open_lock_file(lock_path) as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.seek(0)
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                if deadline is None:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    while True:
                        try:
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("catalog lock is busy") from None
                            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            else:
                import fcntl

                if deadline is None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("catalog lock is busy") from None
                            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
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
    finally:
        thread_lock.release()
