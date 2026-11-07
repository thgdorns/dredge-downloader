"""One live line per lane, updated in place while a pass runs.

rich's ``Live`` owns a block of lines at the bottom of the console; the pass
loop updates it a few times a second. Plain ``print`` calls (the per-URL run
log lines) appear above that block because ``Live`` redirects stdout, so the
board never interleaves with them.

The lane label and the file name are OSC 8 hyperlinks: the label opens the
folder the lane is writing into, the name opens the file. ``legacy_windows`` is
forced off because rich only emits links when it believes the console handles
escape sequences, and paint already turns on virtual-terminal processing.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


def _idle(slot: int) -> Text:
    return Text.assemble((f"lane {slot}: ", LABEL_STYLE), ("idle", "dim"))


# The lane label sits on a teal block, white text, so it reads as the row's
# handle - the part that opens the folder.
LABEL_STYLE = "white on #008080"


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _linked(style: str, uri: str) -> str:
    """``style`` as a rich style string that also carries ``uri``.

    The URI must not contain spaces - ``Path.as_uri()`` percent-encodes them -
    because rich parses a style by splitting on whitespace.
    """
    if not uri:
        return style
    return f"{style} link {uri}" if style else f"link {uri}"


class LaneBoard:
    """A fixed block with one line per lane: URL -> file/speed -> verdict."""

    def __init__(self, console: Console | None = None, linger: float = 3.0) -> None:
        # legacy_windows=False: rich drops OSC 8 hyperlinks in legacy mode, and
        # the lane label/file links are the point of this board on Windows too.
        self.console = console or Console(highlight=False, legacy_windows=False)
        self._linger = linger
        self._done_at: dict[int, float] = {}
        self._lines: dict[int, Text] = {}
        self._paused = False
        self._finishing = 0
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

    def render(self, width: int | None = None) -> Group:
        """The block, cut to the console width *now* (so a resize shows more)."""
        limit = max(20, (width or self.console.width) - 1)
        rows: list[Text] = []
        if self._paused:
            # The queue is held: say so on the board, or a P press looks ignored
            # while the lanes that were already running keep going.
            suffix = (
                f" {self._finishing} lane(s) finishing" if self._finishing else ""
            )
            banner = Text.assemble(
                (" PAUSED ", "bold white on red"),
                (f" new downloads held{suffix} - press P to resume", "yellow"),
            )
            banner.truncate(limit, overflow="ellipsis")
            rows.append(banner)
        for slot in sorted(self._lines):
            text = self._lines[slot].copy()
            text.truncate(limit, overflow="ellipsis")
            rows.append(text)
        return Group(*rows)

    def set_paused(self, paused: bool, finishing: int = 0) -> None:
        """Show whether the queue is held, and how many lanes are still running."""
        self._paused = paused
        self._finishing = finishing

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

    def set_url(
        self, slot: int, index: int, total: int, url: str, folder: str = "",
    ) -> None:
        self._done_at.pop(slot, None)
        self._lines[slot] = Text.assemble(
            (f"lane {slot}: ", _linked(LABEL_STYLE, folder)),
            (f"{index}/{total} ", "dim"),
            # the URL stays plain: the terminal's own link detection opens it
            (url, "cyan"),
        )

    def set_progress(
        self, slot: int, index: int, total: int, name: str, pct: str, speed: str,
        folder: str = "",
    ) -> None:
        parts: list[tuple[str, str]] = [
            (f"lane {slot}: ", _linked(LABEL_STYLE, folder)),
            (f"{index}/{total} ", "dim"),
            (name, "" if _is_url(name) else _linked("", folder)),
        ]
        if pct:
            parts.append((f"  {pct}", "yellow"))
        if speed:
            parts.append((f"  {speed}", "green"))
        self._lines[slot] = Text.assemble(*parts)

    def set_done(
        self, slot: int, index: int, total: int, verdict: str, files: str,
        folder: str = "", target: str = "",
    ) -> None:
        self._done_at[slot] = time.monotonic()
        if verdict.startswith("OK|"):
            style = "green"
        elif verdict == "OFFLINE":
            style = "yellow"
        else:
            style = "red"
        parts: list[tuple[str, str]] = [
            (f"lane {slot}: ", _linked(LABEL_STYLE, folder)),
            (f"{index}/{total} ", "dim"),
            (verdict, style),
        ]
        if files:
            parts.append((files, _linked(style, target or folder)))
        self._lines[slot] = Text.assemble(*parts)
