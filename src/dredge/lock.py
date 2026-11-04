"""One dredge per root.

Two instances share ``queue.txt`` and ``fail.txt`` and overwrite each other's
writes: a pass rewrites the queue with its in-memory snapshot while the menu in
the other process is moving failed links into it - which is how both files can
end up empty. This lock is held for the life of the process; the OS releases it
if the process dies, so a stale file is harmless.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: str = "") -> None:
        super().__init__(pid)
        self.pid = pid


class InstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            fh = self.path.open("r+b")
        else:
            fh = self.path.open("w+b")
        pid = ""
        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"0")          # byte 0 is the lock; never read it
                fh.flush()
            fh.seek(1)
            pid = fh.read(32).decode("ascii", "replace").strip()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(pid) from None

        fh.seek(1)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
