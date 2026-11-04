"""Watch the system clipboard for URLs - no daemon and no CopyQ.

On Windows the clipboard sequence number is polled (a cheap integer) and the
text is read only when it changes. Elsewhere the text is read and compared.
Only http(s) URLs are returned, so passwords or random clipboard text never
reach the queue.

pyperclip uses the native Windows API; on Linux it needs xclip, xsel or
wl-clipboard installed.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ").,;!?'\""


def extract_urls(text: str) -> list[str]:
    """All distinct URLs in a clipboard text, in order."""
    out: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _sequence_number() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001 - platform clipboard APIs fail in many ways
        return None


def available() -> bool:
    """True when a clipboard backend can be imported."""
    try:
        import pyperclip  # noqa: F401 - availability probe
    except ImportError:
        return False
    return True


def _paste() -> str:
    try:
        import pyperclip
    except ImportError:
        return ""

    try:
        return pyperclip.paste() or ""
    except Exception:  # noqa: BLE001 - never let a closed clipboard kill the watcher
        return ""


@dataclass
class ClipboardWatcher:
    interval: float = 0.25
    _last_seq: int | None = None
    _last_hash: str = ""

    def poll(self) -> list[str]:
        """URLs copied since the previous poll (empty when nothing changed)."""
        seq = _sequence_number()
        if seq is not None:
            if seq == self._last_seq:
                return []
            self._last_seq = seq
        text = _paste()
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        if digest == self._last_hash:
            return []
        self._last_hash = digest
        return extract_urls(text)

    def watch(self) -> Iterator[str]:
        while True:
            yield from self.poll()
            time.sleep(self.interval)


class Capture:
    """Background clipboard watcher: copies URLs into ``clip-inbox.txt``.

    The orchestrator drains that inbox at the head of a pass and between lane
    completions, so this thread never touches the queue itself. A single writer
    keeps the file append-only, which is what makes the drain atomic.
    """

    def __init__(self, inbox: str | Path, interval: float = 0.25) -> None:
        self.inbox = Path(inbox)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.inbox.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="clipboard")
        self._thread.start()

    def _run(self) -> None:
        watcher = ClipboardWatcher(interval=self.interval)
        while not self._stop.is_set():
            try:
                urls = watcher.poll()
            except Exception:  # noqa: BLE001 - a clipboard fault must not kill capture
                urls = []
            if urls:
                try:
                    with self.inbox.open("a", encoding="utf-8", newline="") as fh:
                        for url in urls:
                            fh.write(url + "\r\n")
                except OSError:
                    pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
