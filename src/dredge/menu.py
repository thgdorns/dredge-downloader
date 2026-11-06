"""Console menu, plain-input fallback, and the non-blocking menu key.

prompt_toolkit keeps the menu in the existing console (no new window) and gives
key bindings and Esc handling on Windows and Linux. When it is not installed
(contributor machines running only the tests) the menu falls back to ``input()``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import paint


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str
    color: str = ""


def render(title: str, items: Sequence[MenuItem], close: str = "close") -> str:
    lines = [paint.cyan(title), ""]
    for item in items:
        line = f"   {item.key}) {item.label}"
        lines.append(paint.paint(line, item.color) if item.color else line)
    lines.extend(["", paint.darkgray(f"   Esc) {close}")])
    return "\n".join(lines)


def prompt_line(prompt: str) -> str:
    """Read one line; ``""`` on EOF, so redirected input cannot crash a menu."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def select(title: str, items: Sequence[MenuItem], close: str = "close") -> str | None:
    """Show the menu and return the chosen key, or ``None`` for Esc/close."""
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError:
        print(render(title, items, close))
        choice = prompt_line("choice: ").strip().lower()
        return None if choice in ("", "esc", "q") else choice

    bindings = KeyBindings()

    @bindings.add("escape")
    def _close(event):
        event.app.exit(result=None)

    for item in items:

        @bindings.add(item.key)
        def _choose(event, key=item.key):
            event.app.exit(result=key)

    control = FormattedTextControl(lambda: ANSI(render(title, items, close)))
    app = Application(
        layout=Layout(Window(control)),
        key_bindings=bindings,
        full_screen=False,
    )
    return app.run()


def menu_key() -> str | None:
    """A menu/pause key pressed since the last call, without blocking (Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import msvcrt
    except ImportError:
        return None
    key = None
    try:
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            low = ch.lower()
            if low in ("m", "p"):
                key = low
    except OSError:
        return None
    return key


def wait(
    seconds: float,
    on_menu: Callable[[], None] | None = None,
    on_pause: Callable[[], None] | None = None,
) -> None:
    """Sleep that stays responsive to the ``M`` menu and ``P`` pause keys."""
    import time

    end = time.monotonic() + seconds
    while True:
        if on_menu or on_pause:
            key = menu_key()
            if key == "m" and on_menu:
                on_menu()
            elif key == "p" and on_pause:
                on_pause()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.25, left))
