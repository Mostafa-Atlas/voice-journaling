"""Single-instance guard for the voice memo bot.

Discord allows exactly one live gateway session per token. A second copy of
the bot (another terminal, a leftover background process, the systemd service
plus a manual run) makes both copies kick each other's sessions off forever,
each logging only ``session has been invalidated``. This module prevents that
class of failure: the first process holds an OS-level lock for its whole
lifetime, and any later copy exits immediately with a clear error.

The lock is held on an open file handle (``fcntl`` on POSIX, ``msvcrt`` on
Windows), so it can never go stale: the kernel releases it the moment the
owning process dies, however it dies. The PID written inside is diagnostic
only and is never trusted for decisions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import TracebackType

log = logging.getLogger("voicebot.lock")


class AlreadyRunningError(RuntimeError):
    """Raised when another bot process already holds the lock."""


class ProcessLock:
    """Held OS file lock proving this is the only bot process on this data dir."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    @property
    def owner_pid(self) -> int | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            pid = int(text.split()[0])
            return pid if pid > 0 else None
        except (OSError, ValueError, IndexError):
            return None

    def acquire(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(self.path, "a+b")  # noqa: PTH123 - intentional low-level open
        except OSError:
            raise AlreadyRunningError(self._conflict_message()) from None
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0\n")
                handle.flush()
            _lock_nonblocking(handle)
        except OSError:
            handle.close()
            raise AlreadyRunningError(self._conflict_message()) from None
        handle.seek(0)
        handle.truncate(0)
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
        self._handle = handle
        log.info("single-instance lock acquired path=%s", self.path)
        return self

    def _conflict_message(self) -> str:
        owner = self.owner_pid
        hint = f" (lock file claims pid {owner})" if owner else ""
        return (
            "another copy of the bot is already running with this data "
            f"directory{hint}. Running two copies with one Discord token "
            "makes both sessions die with 'session has been invalidated'. "
            "Stop the other copy (`ps aux | grep bot.py`, "
            "`systemctl status voicebot`) and start exactly one. "
            "If no other copy is running, check the data directory permissions."
        )

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                _unlock(handle)
            finally:
                handle.close()

    def __enter__(self) -> ProcessLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _lock_nonblocking(handle) -> None:  # type: ignore[no-untyped-def]
    # Lock byte 0 so every contender meets on the same region.
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        # LK_NBLCK raises OSError if any handle (even ours) holds a lock.
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
