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
import time
from collections.abc import Iterator
from dataclasses import dataclass

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


def _paste() -> str:
    import pyperclip

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
