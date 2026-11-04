"""One live line per lane, updated in place while a pass runs.

rich's ``Live`` owns a block of lines at the bottom of the console; the pass
loop updates it a few times a second. Plain ``print`` calls (the per-URL run
log lines) appear above that block because ``Live`` redirects stdout, so the
board never interleaves with them.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


def _idle(slot: int) -> Text:
    return Text(f"lane {slot}: idle", style="dim")


class LaneBoard:
    """A fixed block with one line per lane: URL -> file/speed -> verdict."""

    def __init__(self, console: Console | None = None, linger: float = 3.0) -> None:
        self.console = console or Console(highlight=False)
        self._linger = linger
        self._done_at: dict[int, float] = {}
        self._lines: dict[int, Text] = {}
        self._live = Live(
            self.render(),
            console=self.console,
            auto_refresh=False,
            transient=True,
            redirect_stdout=True,
        )
        self._running = False

    def start(self) -> None:
        if not self._running:
            self._live.update(self.render(), refresh=False)
            self._live.start()
            self._running = True

    def stop(self) -> None:
        if self._running:
            self._live.stop()
            self._running = False

    def render(self) -> Group:
        return Group(*(self._lines[slot] for slot in sorted(self._lines)))

    def refresh(self) -> None:
        if self._running:
            self._live.update(self.render(), refresh=True)

    def ensure(self, slots: Iterable[int], active: Iterable[int] = ()) -> None:
        """Grow/shrink the block; active lanes keep their line even if reduced."""
        wanted = set(slots) | set(active)
        for slot in [slot for slot in self._lines if slot not in wanted]:
            del self._lines[slot]
            self._done_at.pop(slot, None)
        for slot in wanted:
            if slot not in self._lines:
                self._lines[slot] = _idle(slot)

    def idle(self, slot: int) -> None:
        # keep the verdict visible for a moment before falling back to idle
        if slot in self._done_at and (time.monotonic() - self._done_at[slot]) < self._linger:
            return
        self._done_at.pop(slot, None)
        self._lines[slot] = _idle(slot)

    def set_url(self, slot: int, index: int, total: int, url: str) -> None:
        self._done_at.pop(slot, None)
        self._lines[slot] = self._fit(Text.assemble(
            (f"lane {slot}: ", "dim"),
            (f"{index}/{total} ", "dim"),
            (url, "cyan"),
        ))

    def set_progress(
        self, slot: int, index: int, total: int, name: str, pct: str, speed: str,
    ) -> None:
        parts: list[tuple[str, str]] = [
            (f"lane {slot}: ", "dim"),
            (f"{index}/{total} ", "dim"),
            (name, ""),
        ]
        if pct:
            parts.append((f"  {pct}", "yellow"))
        if speed:
            parts.append((f"  {speed}", "green"))
        self._lines[slot] = self._fit(Text.assemble(*parts))

    def set_done(
        self, slot: int, index: int, total: int, verdict: str, files: str,
    ) -> None:
        self._done_at[slot] = time.monotonic()
        if verdict.startswith("OK|"):
            style = "green"
        elif verdict == "OFFLINE":
            style = "yellow"
        else:
            style = "red"
        label = f"{verdict}{files}" if verdict.startswith("OK|") else verdict
        self._lines[slot] = self._fit(Text.assemble(
            (f"lane {slot}: ", "dim"),
            (f"{index}/{total} ", "dim"),
            (label, style),
        ))

    def _fit(self, text: Text) -> Text:
        text.truncate(max(20, self.console.width - 1), overflow="ellipsis")
        return text
