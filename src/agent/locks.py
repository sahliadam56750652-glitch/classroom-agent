"""Exclusive locks on files under DATA_DIR, released when the process dies.

Two uses (2026-10-17):

  run.lock   held by `agent run` for its whole length and by `agent gate` while
             it brings material up to date itself. Last night the gate and the
             catch-up run fired at the same moment after the PC woke, and the
             gate built its prompt two seconds before the sync started.
  bot.lock   held by `agent bot` for as long as it listens. Two bots polling
             Telegram fight over every update; this makes a second start a
             no-op, however it was started.

An OS lock rather than a pid file: the operating system drops it the instant
the process ends -- crash, kill, power loss -- so a dead holder can never leave
a stale lock behind for someone to delete by hand.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class FileLock:
    """One exclusive lock on one file. Not re-entrant; one holder per process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock if it is free. Never waits."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                # One byte at offset 0. msvcrt locks are byte ranges held by the
                # handle; the OS releases them when the process exits.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(
        self,
        wait_seconds: float,
        *,
        every: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Take the lock, waiting up to `wait_seconds`. False if it never came free."""
        deadline = clock() + wait_seconds
        while True:
            if self.try_acquire():
                return True
            if clock() >= deadline:
                return False
            sleep(every)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def run_lock(data_dir: Path) -> FileLock:
    return FileLock(Path(data_dir) / "run.lock")


def bot_lock(data_dir: Path) -> FileLock:
    return FileLock(Path(data_dir) / "bot.lock")
