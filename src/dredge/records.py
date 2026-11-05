"""The plain-text records: queue, fail, offline, skip.

All of them share one format: one URL per line, blank lines and ``#`` comments
ignored, a UTF-8 BOM tolerated (``.strip()`` does not remove U+FEFF).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .urls import Knowledge

_UTF8 = "utf-8"


def read_lines(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip().lstrip("\ufeff").strip()
        if text and not text.startswith("#"):
            out.append(text)
    return out


def write_lines(path: str | Path, lines: Iterable[str]) -> None:
    """Rewrite a record file as CRLF, UTF-8 without BOM (the legacy format)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(line + "\r\n" for line in lines)
    p.write_text(text, encoding=_UTF8, newline="")


def append_lines(path: str | Path, lines: Iterable[str]) -> int:
    """Append non-blank lines; returns how many were written."""
    keep = [line for line in lines if line.strip()]
    if not keep:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=_UTF8, newline="") as fh:
        for line in keep:
            fh.write(line.strip() + "\r\n")
    return len(keep)


def load_set(path: str | Path) -> set[str]:
    return set(read_lines(path))


def keyed_set(lines: Iterable[str], knowledge: Knowledge) -> set[str]:
    """Keys a URL is matched by: the line itself, its canonical form, its id.

    The legacy skip-set: a raw line matches itself, any URL shape of the same
    item matches the canonical form, and a site with an ``id=`` rule also matches
    every other URL shape that carries the same id.
    """
    keys: set[str] = set()
    for line in lines:
        text = line.strip().lstrip("\ufeff").strip()
        if not text or text.startswith("#"):
            continue
        keys.add(text)
        c = knowledge.canon(text)
        if not c:
            continue
        keys.update(knowledge.keys(text))
        ident = knowledge.seen_id(c)
        if ident:
            keys.add(ident)
    return keys
