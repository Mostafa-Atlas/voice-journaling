"""Apply ``.env`` changes to a running bot without manual restarts.

The bot loads configuration once at startup, so anything printed afterwards
(startup config line, dashboard status, ``!status``) goes stale when ``.env``
changes. The watcher below detects the change and re-executes the process in
place, so the next startup prints the new values. State lives in SQLite and on
disk, so a restart loses nothing but in-flight Discord delays.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

log = logging.getLogger("voicebot.reload")

WATCH_INTERVAL_SECONDS = 5.0


def env_fingerprint(path: Path) -> str:
    """Stable hash of an env file's bytes; ``""`` when missing/unreadable."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def build_exec_args(executable: str | None = None, argv: list[str] | None = None) -> list[str]:
    """Rebuild ``os.execv`` args so the current process restarts identically."""
    exe = executable or sys.executable
    args = list(sys.argv if argv is None else argv)
    if args and args[0] != "-m":
        args[0] = os.path.abspath(args[0])
    return [exe, *args]


def restart_process(reason: str = "") -> NoReturn:
    """Replace this process with a fresh copy of itself (preserves CLI args)."""
    if reason:
        log.warning("restarting process: %s", reason)
    logging.shutdown()
    args = build_exec_args()
    os.execv(args[0], args)
    raise RuntimeError("os.execv returned unexpectedly")  # pragma: no cover


def watch_env_file(
    path: Path,
    *,
    interval: float = WATCH_INTERVAL_SECONDS,
    on_change: Callable[[], None],
    stop: threading.Event | None = None,
) -> threading.Thread:
    """Poll ``path`` in a daemon thread; call ``on_change`` on each change."""
    baseline = env_fingerprint(path)

    def _poll() -> None:
        nonlocal baseline
        while True:
            if stop is not None and stop.is_set():
                return
            time.sleep(interval)
            current = env_fingerprint(path)
            if current == baseline:
                continue
            baseline = current
            try:
                on_change()
            except Exception:  # noqa: BLE001 - watcher must never die loudly
                log.exception("config change handler failed")

    thread = threading.Thread(target=_poll, name="voicebot-env-watch", daemon=True)
    thread.start()
    return thread
