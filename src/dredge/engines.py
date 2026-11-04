"""Engine discovery and process execution.

yt-dlp, gallery-dl, megatools and mediafire_rs are external tools. This module
finds them the way the legacy script did (winget Links, then PATH, then the
winget package folder) and runs them with UTF-8 output, which the frozen exes
need before a non-ASCII title can reach a lane log.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Paths, Settings
from .sites import SiteRule

Sink = Callable[[str], None]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def resolve_engine(exe: str, override: str = "") -> str | None:
    """Path to an engine executable, or ``None``."""
    if override:
        return override if Path(override).exists() else None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        link = Path(local) / "Microsoft" / "WinGet" / "Links" / exe
        if link.exists():
            return str(link)
    found = shutil.which(exe)
    if found:
        return found
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for hit in packages.rglob(exe):
                if hit.is_file():
                    return str(hit)
    return None


def resolve_tool(folder: str, exe: str) -> str | None:
    """Path to a tool installed into its own folder (a version bump needs no edit)."""
    if not folder:
        return None
    p = Path(folder)
    if p.is_file():
        return str(p)
    if not p.is_dir():
        return None
    for hit in p.rglob(exe):
        if hit.is_file():
            return str(hit)
    return None


@dataclass(frozen=True)
class Engines:
    yt: str | None = None
    gdl: str | None = None
    megadl: str | None = None
    mdrs: str | None = None

    @classmethod
    def discover(cls, settings: Settings) -> Engines:
        return cls(
            yt=resolve_engine("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp", settings.yt),
            gdl=resolve_engine(
                "gallery-dl.exe" if sys.platform == "win32" else "gallery-dl", settings.gdl
            ),
            megadl=resolve_tool(
                settings.megatools, "megatools.exe" if sys.platform == "win32" else "megatools"
            ),
            mdrs=resolve_tool(settings.mediafire, "mdrs.exe" if sys.platform == "win32" else "mdrs"),
        )

    def for_rule(self, engine: str) -> str | None:
        return {"megadl": self.megadl, "mdrs": self.mdrs}.get(engine)


def run(
    args: Sequence[str],
    *,
    cwd: str | Path,
    sink: Sink | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    """Run a command, stream its output to ``sink``, return ``(rc, text)``."""
    lines: list[str] = []
    proc = subprocess.Popen(
        list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            if sink:
                sink(line)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        lines.append("[dredge] engine timed out")
        rc = 124
    return rc, "\n".join(lines)


def test_tcp(host: str, port: int = 443, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_down(rule: SiteRule | None, paths: Paths) -> bool:
    """True when none of a site's content servers answer (cached 15 minutes)."""
    if not rule or not rule.probe:
        return False
    paths.state.mkdir(parents=True, exist_ok=True)
    cache = paths.state / f"probe-{rule.host}.cache"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 15 * 60:
        return cache.read_text(encoding="utf-8", errors="replace").strip() == "down"
    up = any(test_tcp(h, 443, 2.5) for h in rule.probe)
    try:
        cache.write_text("up" if up else "down", encoding="utf-8")
    except OSError:
        pass
    return not up


def dir_signature(folder: str | Path) -> str:
    """Cheap fingerprint of a destination's top level (count, bytes, newest)."""
    p = Path(folder)
    if not p.is_dir():
        return "0|0|0"
    count = 0
    total = 0
    newest = 0
    try:
        for item in p.iterdir():
            if item.name.startswith("~"):
                continue          # our own staging folders
            count += 1
            try:
                stat = item.stat()
            except OSError:
                continue
            if not item.is_dir():
                total += stat.st_size
            newest = max(newest, stat.st_mtime_ns)
    except OSError:
        return "0|0|0"
    return f"{count}|{total}|{newest}"


def produced(folder: str | Path, before: str, text: str, dry: bool) -> bool:
    """Some engines exit 0 without writing anything."""
    if dry:
        return True
    if dir_signature(folder) != before:
        return True
    return bool(re.search(r"(?m)^\[download\]\s+Destination:", text))


def lane_status(lane_log: str | Path) -> tuple[str, str]:
    """The file a lane is working on now, and its percentage when known."""
    p = Path(lane_log)
    if not p.exists():
        return "", ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    file = ""
    pct = ""
    patterns = (
        r"\[download\]\s+Destination:\s*(.+)$",
        r"\[Merger\]\s+Merging formats into\s+\"?(.+?)\"?$",
        r"\[gallery-dl\]\s+(.+)$",
        r"\[yt-dlp[^\]]*\]\s+(https?://\S+)$",
        r"\[(?:megadl|mdrs)\]\s+staging\s+(.+)$",
    )
    for line in text.splitlines():
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                file = m.group(1).strip().strip('"')
        m = re.search(r"(\d+(?:\.\d+)?%)\s+of", line)
        if m:
            pct = m.group(1)
    if file and not pct:
        pct = ""
    if file:
        file = re.split(r"[\\/]", file)[-1]
    return file, pct
