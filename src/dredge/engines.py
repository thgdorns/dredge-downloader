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
import threading
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
    gdown: str | None = None

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
            gdown=resolve_engine(
                "gdown.exe" if sys.platform == "win32" else "gdown", settings.gdown
            ),
        )

    def for_rule(self, engine: str) -> str | None:
        return {
            "megadl": self.megadl,
            "mdrs": self.mdrs,
            "gdown": self.gdown,
        }.get(engine)


def run(
    args: Sequence[str],
    *,
    cwd: str | Path,
    sink: Sink | None = None,
    timeout: float | None = None,
    stop_on: str | None = None,
    stall: float | None = None,
) -> tuple[int, str]:
    """Run a command, stream its output to ``sink``, return ``(rc, text)``.

    ``stop_on`` kills the process as soon as a line matches the pattern. It is
    for conditions the engine retries forever instead of failing, such as a
    MEGA "509 over quota" answer: megatools then sleeps for minutes per chunk
    and the lane would stay busy until the quota resets.

    ``stall`` is the silent case: an engine that prints nothing at all (a
    stalled socket, a hung megatools) never trips ``stop_on`` and never returns,
    so a watchdog kills it after that many seconds without output.
    """
    lines: list[str] = []
    stop = re.compile(stop_on) if stop_on else None
    # The child's own stdout encoding, not just the pipe's: gdown prints the
    # filenames it is about to write, and a drive folder with Japanese names
    # dies on the Windows ANSI code page before the first byte is fetched.
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_NO_WINDOW,
    )
    heartbeat = [time.monotonic()]
    watchdog: threading.Thread | None = None
    if stall and stall > 0:
        def _watchdog() -> None:
            while proc.poll() is None:
                time.sleep(0.5)
                if time.monotonic() - heartbeat[0] > stall:
                    lines.append(
                        f"[dredge] engine stopped: no output for {stall:g}s"
                    )
                    proc.kill()
                    return

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            heartbeat[0] = time.monotonic()
            line = raw.rstrip("\r\n")
            lines.append(line)
            if sink:
                sink(line)
            if stop is not None and stop.search(line):
                lines.append("[dredge] engine stopped: it cannot make progress")
                proc.kill()
                break
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        lines.append("[dredge] engine timed out")
        rc = 124
    if watchdog is not None:
        watchdog.join(timeout=1.0)
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


def _number(value: str) -> float | None:
    """A byte count/speed: raw digits or gallery-dl's ``4.78M`` style."""
    text = value.strip().upper()
    if not text or text in ("NA", "NONE"):
        return None
    factor = 1.0
    for suffix, scale in (("K", 1024.0), ("M", 1024.0 ** 2), ("G", 1024.0 ** 3),
                          ("T", 1024.0 ** 4)):
        if text.endswith(suffix):
            factor = scale
            text = text[:-1]
            break
    try:
        return float(text) * factor
    except ValueError:
        return None


def _rate(value: str) -> str:
    """Human speed from bytes/s; "" when unknown."""
    speed = _number(value)
    if speed is None or speed <= 0:
        return ""
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if speed < 1024 or unit == "GiB/s":
            return f"{speed:.1f} {unit}"
        speed /= 1024
    return ""


def _percent(done: str, total: str) -> str:
    downloaded = _number(done)
    size = _number(total)
    if downloaded is None or size is None or size <= 0:
        return ""
    return f"{downloaded * 100 / size:.1f}%"


def lane_status(lane_log: str | Path) -> tuple[str, str, str]:
    """The file a lane is working on now, its percentage and speed when known."""
    p = Path(lane_log)
    if not p.exists():
        return "", "", ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", "", ""
    return _parse_lane_text(text)


def lane_status_tail(
    lane_log: str | Path, *, head: int = 4096, tail: int = 8192
) -> tuple[str, str, str]:
    """Same as :func:`lane_status`, but only reads the ends of a big lane log."""
    p = Path(lane_log)
    if not p.exists():
        return "", "", ""
    try:
        with p.open("rb") as fh:
            first = fh.read(head)
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            last = fh.read()
    except OSError:
        return "", "", ""
    return _parse_lane_text((first + b"\n" + last).decode("utf-8", "replace"))


def _speed(value: str) -> str:
    """megatools prints human units (``969.8 KiB/s``), not raw bytes."""
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)/s", value.strip())
    if not m:
        return ""
    factors = {
        "b": 1, "byte": 1, "bytes": 1,
        "kb": 1000, "kib": 1024,
        "mb": 1000 ** 2, "mib": 1024 ** 2,
        "gb": 1000 ** 3, "gib": 1024 ** 3,
    }
    factor = factors.get(m.group(2).lower())
    if factor is None:
        return ""
    return _rate(str(float(m.group(1)) * factor))


def _parse_lane_text(text: str) -> tuple[str, str, str]:
    file = ""
    pct = ""
    speed = ""
    patterns = (
        r"\[download\]\s+Destination:\s*(.+)$",
        r"\[Merger\]\s+Merging formats into\s+\"?(.+?)\"?$",
        r"\[gallery-dl\]\s+(.+)$",
        r"\[yt-dlp[^\]]*\]\s+(https?://\S+)$",
        r"\[(?:megadl|mdrs)\]\s+staging\s+(.+)$",
        r"^((?:[A-Za-z]:[\\/]|/)[^\r\n]+)$",
    )
    for line in text.splitlines():
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                file = m.group(1).strip().strip('"')
        # megatools: "name.mp4: 27.03% - 256.0 MiB (268435456 bytes) of ... (969.8 KiB/s)"
        m = re.search(r"^(.+?):\s+(\d+(?:\.\d+)?)%.*?\(([^)]*?/s)\)\s*$", line)
        if m:
            file = m.group(1).strip()
            pct = f"{float(m.group(2)):.1f}%"
            speed = _speed(m.group(3))
        m = re.search(r"(\d+(?:\.\d+)?%)\s+of", line)
        if m:
            pct = m.group(1)
        # gdown prints the target path, then a tqdm bar:
        #   To: G:\Downloads\clip.mp4
        #     1%|          | 7.86M/1.22G [00:00<01:24, 14.3MB/s]
        m = re.search(r"^To:\s*(.+)$", line)
        if m:
            file = m.group(1).strip()
        m = re.search(
            r"(\d{1,3})%\|[^|]*\|\s*\S+/\S+\s*\[[^\]]*?,\s*([\d.]+\s*[A-Za-z]+/s)\]",
            line,
        )
        if m:
            pct = f"{int(m.group(1))}%"
            speed = _speed(m.group(2))

    # Progress markers written by our engine arguments. The lane log may carry
    # many updates on one line (\r separated), so take the last match.
    marks = list(re.finditer(r"dredge-yt\s+(\S+)\s+(\S+)\s+(\S+)", text))
    if marks:
        downloaded, total, rate = marks[-1].groups()
        pct = _percent(downloaded, total) or pct
        speed = _rate(rate)

    marks = list(re.finditer(r"dredge-gdl\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+))?", text))
    if marks:
        downloaded, rate, total, percent = marks[-1].groups()
        if percent:
            try:
                pct = f"{float(percent):.0f}%"
            except ValueError:
                pct = _percent(downloaded, total) or pct
        else:
            pct = _percent(downloaded, total) or pct
        speed = _rate(rate)

    if file and not re.match(r"(?i)^https?://", file):
        # A path or a filename is shown by its last segment; a URL is shown as
        # the whole URL, so the lane says which post/creator is being worked on.
        file = re.split(r"[\\/]", file)[-1]
    return file, pct, speed
