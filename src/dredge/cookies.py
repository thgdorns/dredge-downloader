"""Refresh ``config/cookies.txt`` from the browser that is logged in.

Every startup exports the browser's cookies through gallery-dl, so the engines
always use a current members-only session. Firefox-family browsers (Floorp,
Firefox) can be read while they are open; Chromium-based ones hold an exclusive
lock on the cookie database and must be closed.

A request cannot carry an unlimited number of cookies, and a site that sets one
per page can push its own ``Cookie`` header past what its server accepts. A host
that must not be sent some of them says so in ``sites.txt`` (``cookiedrop=``).

On any failure the existing ``cookies.txt`` is kept untouched.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path

from .config import Paths
from .engines import run
from .urls import Knowledge

# A real export is far bigger than this; a failed run leaves nothing or a stub.
_MIN_BYTES = 100

_EXPORT_URL = "https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg"


def drop_blocked(lines: Iterable[str], knowledge: Knowledge) -> tuple[list[str], int]:
    """Drop cookie lines a site must not receive; returns ``(kept, dropped)``.

    Netscape cookie format: ``domain  flag  path  secure  expiry  name  value``.
    ``#HttpOnly_`` is a prefix on a real line, not a comment, so it is removed
    before the fields are read and the line itself is kept verbatim.
    """
    kept: list[str] = []
    dropped = 0
    for line in lines:
        body = line
        if body.startswith("#HttpOnly_"):
            body = body[len("#HttpOnly_"):]
        elif body.startswith("#") or not body.strip():
            kept.append(line)
            continue
        fields = body.split("\t")
        if len(fields) < 7:
            kept.append(line)
            continue
        rule = knowledge.rule_for_host(fields[0].strip().lstrip(".").lower())
        if rule and any(fnmatch.fnmatch(fields[5], pat) for pat in rule.cookiedrop):
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def refresh(paths: Paths, knowledge: Knowledge, gdl_exe: str | None, browser: str) -> str:
    """Export the browser's cookies; returns the console message to print."""
    if not gdl_exe or not browser:
        return "Cookie refresh skipped (no gallery-dl or no browser configured)."
    target = paths.cookies
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    run(
        [
            gdl_exe, "--config-ignore", "--cookies-from-browser", browser,
            "--cookies-export", str(tmp), "--simulate", _EXPORT_URL,
        ],
        cwd=paths.root,
    )
    try:
        size = tmp.stat().st_size
    except OSError:
        size = 0
    if size <= _MIN_BYTES:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "Cookie refresh failed - keeping existing cookies.txt."

    try:
        lines = tmp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "Cookie refresh failed - keeping existing cookies.txt."
    kept, dropped = drop_blocked(lines, knowledge)
    try:
        tmp.write_text(
            "".join(line + "\n" for line in kept), encoding="utf-8", newline=""
        )
        tmp.replace(target)
    except OSError:
        return "Cookie refresh failed - keeping existing cookies.txt."
    if dropped:
        return (
            f"Cookies refreshed from {browser}; dropped {dropped} cookie(s) a site "
            "must not receive (cookiedrop= in sites.txt)."
        )
    return f"Cookies refreshed from {browser}."


def read_cookie_names(path: str | Path) -> set[str]:
    """Cookie names present in a Netscape cookie file (for menus and diagnostics)."""
    p = Path(path)
    if not p.exists():
        return set()
    names: set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        body = line.removeprefix("#HttpOnly_")
        if body.startswith("#") or not body.strip():
            continue
        fields = body.split("\t")
        if len(fields) >= 7:
            names.add(fields[5])
    return names
