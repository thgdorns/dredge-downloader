"""ANSI colour for console output, matching the legacy Write-Host palette.

Colour is only emitted on an interactive console: pipes, log files and the test
suite stay plain text. ``NO_COLOR`` (https://no-color.org) turns it off and
``FORCE_COLOR=1`` turns it on for a redirected run (``dredge | less -R``).
On Windows the console also needs virtual-terminal processing enabled, which is
the only reason this module talks to the kernel32 console API.
"""

from __future__ import annotations

import os
import re
import sys

RESET = "\x1b[0m"

# The four Write-Host names the script used, plus the two greys.
CODES = {
    "cyan": "36",
    "green": "32",
    "yellow": "33",
    "red": "31",
    "gray": "37",
    "darkgray": "90",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_vt: bool | None = None


def _windows_vt() -> bool:
    """Turn on ANSI escape handling for this console, once per process."""
    global _vt
    if _vt is not None:
        return _vt
    if os.name != "nt":
        _vt = True
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for std in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(std)
            if not handle or handle == -1:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        _vt = True
    except (AttributeError, OSError, ValueError):
        _vt = False
    return _vt


def enabled() -> bool:
    """True when colour codes belong in the output."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    forced = os.environ.get("FORCE_COLOR", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        _windows_vt()
        return True
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    return _windows_vt()


def paint(text: str, color: str) -> str:
    """``text`` in ``color`` when colour is on; unchanged otherwise."""
    code = CODES.get(color)
    if not text or not code or not enabled():
        return text
    return f"\x1b[{code}m{text}{RESET}"


def strip(text: str) -> str:
    """``text`` without ANSI codes, for the console mirror log."""
    return _ANSI.sub("", text)


def cyan(text: str) -> str:
    return paint(text, "cyan")


def green(text: str) -> str:
    return paint(text, "green")


def yellow(text: str) -> str:
    return paint(text, "yellow")


def red(text: str) -> str:
    return paint(text, "red")


def gray(text: str) -> str:
    return paint(text, "gray")


def darkgray(text: str) -> str:
    return paint(text, "darkgray")
