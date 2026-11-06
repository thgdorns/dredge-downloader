"""Filesystem layout and runtime settings for a dredge installation.

Nothing here is machine-specific: everything derives from ``root`` (the folder
that holds ``config/``, ``data/`` and ``logs/``), from ``config/dredge.conf`` or
from environment variables. The defaults match the legacy PowerShell script, so
pointing both at the same root produces the same decisions.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# An unavailable/deleted item is not retried: it is gone, not throttled.
OFFLINE_RE = re.compile(
    r"(?i)(no longer available|unavailable|not found|removed|deleted|404|"
    r"does not exist|offline)"
)

# Temporary trouble: rate limits, maintenance pages, gateway errors, a bot
# challenge. The URL must stay retryable and must never be filed as a dead post.
# Checked before OFFLINE_RE, which would otherwise treat "Service Unavailable"
# (or a 503 page) as "unavailable" and park the link forever.
RATE_RE = re.compile(
    r"(?i)(429|too many requests|rate limit|too many attempts|try again later|"
    r"slow down|\b50[0234]\b|service unavailable|temporarily unavailable|"
    r"down for maintenance|under maintenance|maintenance|bad gateway|"
    r"gateway time-?out|connection (?:reset|refused|aborted)|timed out|"
    r"timeout|cloudflare|just a moment|checking your browser)"
)

MEDIA_EXT = (
    "mp4|mkv|webm|mov|avi|flv|wmv|m4v|ts|mpg|mpeg|3gp|mp3|m4a|flac|wav|opus|"
    "aac|jpg|jpeg|png|gif|webp|bmp|pdf|zip|rar|7z|tar|gz|tgz|iso|psd|clip|sai|"
    "kra|bin|txt"
)

# What a yt-dlp tier may record. A page asset (.css, .js, a preview .jpg) is
# not a download, and without this a generic pass happily reports one.
AV_EXT = (
    "mp4|mkv|webm|mov|avi|flv|wmv|m4v|ts|mpg|mpeg|3gp|mp3|m4a|flac|wav|opus|aac"
)


@dataclass(frozen=True)
class Paths:
    """Where every file dredge reads or writes lives."""

    root: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> Paths:
        base = Path(root or os.environ.get("DREDGE_ROOT") or ".").expanduser().resolve()
        return cls(base)

    # -- folders ---------------------------------------------------------
    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    # -- configuration ---------------------------------------------------
    @property
    def settings(self) -> Path:
        return self.config / "dredge.conf"

    @property
    def sites(self) -> Path:
        return self.config / "sites.txt"

    @property
    def mirrors(self) -> Path:
        return self.config / "mirrors.txt"

    @property
    def filehosts(self) -> Path:
        return self.config / "filehosts.txt"

    @property
    def generic_sites(self) -> Path:
        return self.config / "generic-sites.txt"

    @property
    def ytdlp_conf(self) -> Path:
        return self.config / "ytdlp.conf"

    @property
    def ytdlp_generic_conf(self) -> Path:
        return self.config / "ytdlp-generic.conf"

    @property
    def gallery_conf(self) -> Path:
        return self.config / "gallery-dl.conf"

    @property
    def gallery_runtime(self) -> Path:
        return self.config / "gallery-dl.runtime.json"

    @property
    def cookies(self) -> Path:
        return self.config / "cookies.txt"

    # -- records ---------------------------------------------------------
    @property
    def queue(self) -> Path:
        return self.data / "queue.txt"

    @property
    def archive(self) -> Path:
        return self.data / "downloads.tsv"

    @property
    def fail(self) -> Path:
        return self.data / "fail.txt"

    @property
    def offline(self) -> Path:
        return self.data / "offline.txt"

    @property
    def skip(self) -> Path:
        return self.data / "skip.txt"

    @property
    def fold_pending(self) -> Path:
        return self.state / "fold-pending.tsv"

    @property
    def clip_inbox(self) -> Path:
        return self.state / "clip-inbox.txt"

    @property
    def paused(self) -> Path:
        return self.state / "paused"

    @property
    def lock(self) -> Path:
        return self.state / "dredge.lock"

    @property
    def staged_queue(self) -> Path:
        return self.state / "queue-requeue.txt"

    @property
    def gallery_archive(self) -> Path:
        return self.state / "gallery-dl.sqlite3"

    def lane_log(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.log"

    def lane_result(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.result"

    def lane_archive(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.archive"

    def lane_files(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.files"

    def lane_dest(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.dest"

    def lane_extractor(self, slot: int) -> Path:
        return self.logs / f"lane{slot}.extractor"

    def ensure(self) -> None:
        for folder in (self.data, self.state, self.logs):
            folder.mkdir(parents=True, exist_ok=True)


def write_gallery_runtime(paths: Paths, settings: Settings) -> None:
    """Regenerate ``config/gallery-dl.runtime.json`` (the creator-folder rule).

    It is passed to gallery-dl *after* its main config so the rule applies even
    to an older or hand-copied ``gallery-dl.conf``. ``keywords-default`` makes a
    missing key count as empty instead of the literal string ``None``.
    """
    payload = {
        "output": {
            # Progress markers for the lane status (speed). Newline-terminated so
            # the line is readable while the download runs; the format strings
            # are gallery-dl's {0}=bytes, {1}=bytes/s, {2}=total, {3}=percent.
            "mode": {
                "start": "{}\n",
                "success": "{}\n",
                "skip": "# {}\n",
                "progress": "dredge-gdl {0} {1} {2}\n",
                "progress-total": "dredge-gdl {0} {1} {2} {3}\n",
            }
        },
        "extractor": {
            "keywords-default": "",
            "directory": [settings.gdl_dir],
        }
    }
    try:
        paths.config.mkdir(parents=True, exist_ok=True)
        paths.gallery_runtime.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _as_path(value: str, root: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


@dataclass(frozen=True)
class Destination:
    """A named root: where files go and how they are named inside it."""

    name: str
    path: Path
    layout: str = "creator"      # "creator" = one folder per uploader; "flat"
    tpl: str = ""                # yt-dlp template; empty = the layout default


@dataclass
class Settings:
    """Runtime switches and the machine-specific values (destinations, templates).

    Read from ``config/dredge.conf`` when it exists; every key is optional.
    Command-line flags override the file, the file overrides these defaults.
    """

    root: Path
    dest_default: Path = Path("E:/Downloads")
    dest_flat: Path = Path("E:/Downloads/misc")
    destinations: dict[str, Destination] = field(default_factory=dict)
    default_dest: str = "default"
    dest_fallback: tuple[str, ...] = ("D:", "G:")
    dest_fallback_below_gb: int = 20

    tpl_default: str = "%(uploader,channel,creator,artist,extractor|NA)s/%(title).200s.%(ext)s"
    tpl_flat: str = "%(title).200s.%(ext)s"
    tpl_generic: str = "%(title).200s.%(ext)s"
    gdl_dir: str = (
        "{tag_string_artist|tags_artist[0]|uploader|uploader_name|channel|artist|"
        "creator[full_name]|creator[vanity]|creator[name]|creator[username]|"
        "creator|username|user[name]|author[name]|author|account[username]|category}"
    )

    max_downloads: int = 4
    yt: str = ""
    gdl: str = ""
    megatools: str = "D:\\Warez\\megatools"
    mediafire: str = "D:\\Warez\\mediafire_rs"
    cookies_browser: str = "floorp"

    # switches
    dry_run: bool = False
    once: bool = False
    idle_seconds: int = 3
    no_update: bool = False
    no_cookies: bool = False
    retry_failed: bool = False
    menu: bool = False
    probe: bool = False
    probe_files: bool = False
    strict_generic: bool = False
    no_capture: bool = False
    no_prompt: bool = False
    ignore_records: bool = False
    progress_every_seconds: int = 15

    # batching
    fold_batch_rows: int = 25
    fold_batch_seconds: int = 60

    # destinations resolved after the drive fallback, for reference in menus
    fallback_note: str = ""

    def __post_init__(self) -> None:
        if not self.destinations:
            self.destinations = {
                "default": Destination("default", self.dest_default, "creator"),
                "flat": Destination("flat", self.dest_flat, "flat"),
            }
        if self.default_dest not in self.destinations:
            self.default_dest = (
                "default" if "default" in self.destinations
                else next(iter(self.destinations), "default")
            )

    @classmethod
    def load(cls, paths: Paths, **overrides) -> Settings:
        values: dict[str, object] = {}
        sections: dict[str, dict[str, str]] = {}
        conf = paths.settings
        if conf.exists():
            values, sections = _read_conf(conf)
        values = {k: v for k, v in values.items() if v is not None}
        root = paths.root
        for key in ("dest_default", "dest_flat"):
            if key in values:
                values[key] = _as_path(str(values[key]), root)
        if "dest_fallback" in values and isinstance(values["dest_fallback"], str):
            values["dest_fallback"] = tuple(
                part.strip() for part in str(values["dest_fallback"]).split(",") if part.strip()
            )
        default_name = str(values.pop("destination_default", "") or "").strip().lower()

        built: dict[str, Destination] = {}
        for name, raw in sections.items():
            path = raw.get("path", "").strip()
            if not path:
                continue
            layout = raw.get("layout", "creator").strip().lower()
            built[name] = Destination(
                name=name,
                path=_as_path(path, root),
                layout="flat" if layout == "flat" else "creator",
                tpl=raw.get("tpl", "").strip(),
            )
        if built:
            values["destinations"] = built
        if default_name:
            values["default_dest"] = default_name

        values.update({k: v for k, v in overrides.items() if v is not None})

        return cls(root=root, **values)  # type: ignore[arg-type]


def _read_conf(path: Path) -> tuple[dict[str, object], dict[str, dict[str, str]]]:
    """Read the config: top-level ``key = value`` plus ``[dest:NAME]`` sections."""
    known = {
        "dest_default": str, "dest_flat": str,
        "destination_default": str, "dest_fallback": str,
        "dest_fallback_below_gb": int, "tpl_default": str, "tpl_flat": str,
        "tpl_generic": str, "gdl_dir": str, "max_downloads": int, "yt": str,
        "gdl": str, "megatools": str, "mediafire": str, "cookies_browser": str,
        "idle_seconds": int, "progress_every_seconds": int,
        "fold_batch_rows": int, "fold_batch_seconds": int,
    }
    out: dict[str, object] = {}
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("[") and text.endswith("]"):
            name = text[1:-1].strip().lower()
            current = sections.setdefault(name[5:], {}) if name.startswith("dest:") else None
            continue
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip().lower().replace("-", "_")
        value = value.strip().strip('"').strip("'")
        kind = known.get(key)
        if current is not None and kind is None:
            current[key] = value
            continue
        if kind is None:
            continue
        try:
            out[key] = kind(value) if kind is not int else int(value)
        except ValueError:
            continue
    return out, sections


def save_setting(path: str | Path, key: str, value: str) -> None:
    """Set ``key = value`` in a config file, keeping every other line.

    The menu uses this for the lane count, so it lives with the rest of the
    per-user settings instead of a separate state file.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    pattern = re.compile(rf"(?im)^[ \t]*{re.escape(key)}[ \t]*=.*$")
    line = f"{key} = {value}"
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + line + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    except OSError:
        pass


@dataclass
class Runtime:
    """Values the run changes: lane count, records-check switch, pause state."""

    paths: Paths
    settings: Settings
    max_downloads: int = field(default=4)
    archive_check: bool = True
    strict_generic: bool = False
    pause_shown: bool = False
    # record sets, kept current by state.py
    seen: set[str] = field(default_factory=set)
    archived: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    skip_always: set[str] = field(default_factory=set)
    arc_loaded: bool = False
    arc_sig: str = ""

    def __post_init__(self) -> None:
        self.max_downloads = max(1, int(self.settings.max_downloads))
        if not self.strict_generic:
            self.strict_generic = self.settings.strict_generic
