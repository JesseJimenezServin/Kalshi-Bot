"""
Only one bot process per database.

The market maker ran several unaware copies at once by accident and
corrupted a data window with concurrent writers (its 2026-09-01 incident).
An OS file lock is released automatically if the process dies, so there is
no stale-lock cleanup to get wrong.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


class AlreadyRunning(RuntimeError):
    pass


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise AlreadyRunning(f"another instance holds {self.path} (pid {holder})") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> SingletonLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
