"""Destination routing: adult streaming-video sites to the porn root, the rest default.

A file-hosting link names a file, not a creator, so it is written into
``<destination>/<folder>`` instead of loose in the destination root. The folder
comes from a ``folder=`` regex in ``sites.txt`` or from a path segment that reads
as a name. MEGA folder links carry the name in encrypted attributes; this port
falls back to the folder id (the legacy script decrypts it - see README).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from .config import Settings
from .urls import Knowledge, host

_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_SKIP_SEGMENTS = {
    "folder", "folders", "dir", "file", "files", "d", "l", "f", "view",
    "download", "archive", "public", "share", "s", "u",
}
_MEGA_FOLDER = re.compile(r"(?i)/folder/([A-Za-z0-9_-]+)(?:#([A-Za-z0-9_-]+))?")


def load_domains(path: str | Path) -> tuple[str, ...]:
    """One domain per line, ``#`` comments ignored."""
    p = Path(path)
    if not p.exists():
        return ()
    out = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip().lower()
        if text and not text.startswith("#"):
            out.append(text.removeprefix("www."))
    return tuple(out)


def is_porn(url: str, domains: tuple[str, ...]) -> bool:
    h = host(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def destination(url: str, settings: Settings, domains: tuple[str, ...]) -> Path:
    return settings.dest_porn if is_porn(url, domains) else settings.dest_default


def filehost_folder(
    url: str,
    knowledge: Knowledge,
    filehosts: tuple[str, ...],
    *,
    mega_name: str | None = None,
) -> str:
    """Subfolder for a file-host collection link, ``""`` for a single file."""
    h = host(url)
    if not h:
        return ""
    bare = h.removeprefix("www.")
    if not any(bare == d or bare.endswith("." + d) for d in filehosts):
        return ""

    if bare in ("mega.nz", "mega.io"):
        m = _MEGA_FOLDER.search(url)
        if not m:
            return ""
        name = mega_name or m.group(1)
        return _clean(name) or m.group(1)

    rule = knowledge.rule("http://" + bare)
    if rule and rule.folder:
        m = re.search(rule.folder, url)
        if not m:
            return ""                      # a single-file link: no collection
        return _clean(unquote(m.group(1)))

    path = re.sub(r"^[a-z]+://[^/]+", "", url, flags=re.IGNORECASE)
    path = re.sub(r"[?#].*$", "", path)
    candidates: list[str] = []
    for segment in path.split("/"):
        segment = unquote(segment).strip()
        if not segment:
            continue
        low = segment.lower()
        if low in _SKIP_SEGMENTS:
            continue
        if re.search(r"\.[A-Za-z0-9]{2,5}$", segment):
            continue
        if re.fullmatch(r"[0-9a-fA-F-]{8,}", segment):
            continue
        if re.fullmatch(r"[A-Za-z0-9]{6,}", segment):
            continue
        candidates.append(segment)
    if not candidates:
        return ""
    name = _clean(candidates[-1])
    return name if len(name) >= 2 else ""


def _clean(name: str) -> str:
    return _UNSAFE.sub("_", name).strip().rstrip(".")


def free_gb(path: str | Path) -> float:
    """Free GB on the drive holding ``path``; ``-1`` when it is not mounted."""
    p = Path(path)
    root = Path(p.anchor) if p.anchor else p
    try:
        return shutil.disk_usage(root).free / 1024**3
    except OSError:
        return -1.0


def drive_letters(value: str) -> tuple[str, ...]:
    out = []
    for part in value.split(","):
        part = part.strip().rstrip("\\")
        if re.fullmatch(r"[A-Za-z]:", part):
            out.append(part.upper())
    return tuple(out)


def apply_fallback(settings: Settings) -> Settings:
    """Point both destinations at a fallback drive when the primary is unusable.

    Same tree, only the drive letter changes, so the libraries stay mergeable.
    A candidate is skipped when it is not mounted; the first one with enough free
    space wins, otherwise the roomiest beats a worse primary.
    """
    drives = drive_letters(",".join(settings.dest_fallback))
    if not drives:
        return settings
    free_now = free_gb(settings.dest_porn)
    gone = free_now < 0
    chosen: str | None = None
    chosen_free = -1.0
    for drive in drives:
        free = free_gb(drive + "\\")
        if free <= 0:
            continue
        if free >= settings.dest_fallback_below_gb:
            chosen, chosen_free = drive, free
            break
        if free > chosen_free:
            chosen, chosen_free = drive, free
    if not chosen:
        return settings
    below = free_now < settings.dest_fallback_below_gb
    if not (gone or (below and chosen_free > free_now)):
        return settings

    def move(p: Path) -> Path:
        parts = p.parts
        if not parts or not re.fullmatch(r"[A-Za-z]:", parts[0].rstrip("\\/")):
            return p
        return Path(chosen + "\\").joinpath(*parts[1:])

    if gone:
        note = f"destination drive is not mounted - writing to {chosen} instead, same tree"
    else:
        note = (
            f"destination drive has only {free_now:.0f} GB free - writing to "
            f"{chosen} instead, same tree"
        )
    return replace(
        settings,
        dest_default=move(settings.dest_default),
        dest_porn=move(settings.dest_porn),
        fallback_note=note,
    )
