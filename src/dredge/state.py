"""Record sets and the small file operations the menu and the pass share.

The legacy script kept these as script-scope hashtables (``$seen``, ``$failed``,
``$skipAlways``). Here they live on :class:`~dredge.config.Runtime`, rebuilt by
:func:`update_archive_sets` / :func:`update_failed_set`.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import records
from .archive import load_sets
from .config import Paths, Runtime, save_setting
from .urls import Knowledge


def save_lanes(paths: Paths, value: int) -> None:
    """Write the menu's lane count back to config/dredge.conf."""
    save_setting(paths.settings, "max_downloads", str(value))


def archive_signature(path: str | Path) -> str:
    p = Path(path)
    try:
        stat = p.stat()
    except OSError:
        return ""
    return f"{stat.st_size}|{stat.st_mtime_ns}"


def update_archive_sets(runtime: Runtime, knowledge: Knowledge) -> None:
    runtime.seen, runtime.archived = load_sets(runtime.paths.archive, knowledge)
    runtime.arc_sig = archive_signature(runtime.paths.archive)
    runtime.arc_loaded = True


def update_failed_set(runtime: Runtime, knowledge: Knowledge) -> None:
    """Rebuild the known-bad sets from skip.txt, offline.txt and fail.txt."""
    paths = runtime.paths
    runtime.skip_always = records.keyed_set(records.read_lines(paths.skip), knowledge)
    failed: set[str] = set()
    for path in (paths.offline, paths.fail):
        failed |= records.keyed_set(records.read_lines(path), knowledge)
    runtime.failed = failed


def add_queue_lines(path: str | Path, urls: list[str]) -> int:
    return records.append_lines(path, urls)


def read_clip_inbox(path: str | Path) -> list[str]:
    """Drain the clipboard inbox atomically (another writer may hold it)."""
    p = Path(path)
    if not p.exists():
        return []
    taken = p.with_suffix(p.suffix + ".draining")
    try:
        p.replace(taken)
    except OSError:
        return []
    try:
        return records.read_lines(taken)
    finally:
        try:
            taken.unlink()
        except OSError:
            pass


def wait_for_change(
    queue: Path,
    inbox: Path,
    seconds: float = 0.25,
    sleep=None,
) -> None:
    """Idle wait: return only when the queue changes or a capture is waiting.

    There is no timeout: the caller prints its "waiting" line once and this
    sleeps (``seconds`` per tick) until there is something to do, so an idle
    dredge does not repeat the message every few seconds. ``sleep`` is a
    callable that waits one tick (the menu-aware sleeper in the CLI), so the
    menu key still works.
    """
    sleeper = sleep or time.sleep
    try:
        signature = queue.stat().st_mtime_ns
    except OSError:
        signature = 0
    while True:
        if inbox.exists():
            return
        try:
            if queue.stat().st_mtime_ns != signature:
                return
        except OSError:
            pass
        sleeper(seconds)
