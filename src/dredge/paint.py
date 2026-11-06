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
from pathlib import Path

RESET = "\x1b[0m"

# The four Write-Host names the script used, plus the two greys. The extra
# bright entries are what the terminal uses for timestamps (blue) and URL
# verdicts (light green / light red); standard 31/32 are too dark for a URL
# that has to stand out from the rest of the line.
CODES = {
    "cyan": "36",
    "green": "32",
    "yellow": "33",
    "red": "31",
    "gray": "37",
    "darkgray": "90",
    "blue": "94",
    "lightgreen": "92",
    "lightred": "91",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# OSC 8 hyperlinks (``ESC ] 8 ; params ; URI ST``): the URI is invisible in the
# text, so it has to be removed when the output is mirrored to a log file.
_OSC8 = re.compile(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)")
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
    return _OSC8.sub("", _ANSI.sub("", text))


def link(text: str, url: str) -> str:
    """``text`` as a clickable hyperlink (OSC 8), when colour is on.

    ``url`` may be http(s) or ``file:`` - the terminal's own handler decides
    what to do with it, which is why the same code works on Windows and Linux.
    The URI is written with the text, so ``strip`` removes it again for logs.
    """
    if not text or not url or not enabled():
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


_URL_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/:@!$&'()*+,;="
)


def file_uri(path: str | Path) -> str:
    """A ``file://`` URI both Windows and Linux open, or ``""``.

    Non-ASCII stays literal on purpose: the Windows shell reads %XX escapes in
    a ``file:`` URI as ANSI bytes, so a percent-encoded CJK path resolves to
    nothing, while the same path written as UTF-8 opens correctly (checked
    against the shell's own name parser, ``SHParseDisplayName``). Spaces and
    the other reserved characters are still escaped.
    """
    target = Path(path)
    if not target.is_absolute():
        return ""
    posix = target.as_posix()
    if posix.startswith("//"):          # a UNC share: \\server\share
        prefix = "file:"
    else:
        prefix = "file://"
        posix = "/" + posix
    encoded = "".join(
        ch if ch in _URL_SAFE or ord(ch) > 0x7F
        else "".join(f"%{byte:02X}" for byte in ch.encode("utf-8"))
        for ch in posix
    )
    return prefix + encoded


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


def blue(text: str) -> str:
    return paint(text, "blue")


def lightgreen(text: str) -> str:
    return paint(text, "lightgreen")


def lightred(text: str) -> str:
    return paint(text, "lightred")
