"""One URL, the whole tier chain, one verdict.

The worker is deliberately engine-agnostic: gallery-dl first, then yt-dlp with
its dedicated extractors, then yt-dlp again with its own downloader, then the
all-extractor pass. The next tier only runs when the previous one exited non-zero
or produced nothing. A host never picks an engine; the only skip is
``engine=gdl`` in ``sites.txt`` and "gallery-dl knows this creator page".

Verdicts: ``OK|<engine>``, ``OK|expanded`` (a flat listing staged its item URLs
for the next pass), ``PREVIEW|<engine>`` (the archive has only thumbnails, so
the post text was saved and the link is parked), ``FAILED``, ``OFFLINE``.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .archive import format_row
from .config import AV_EXT, MEDIA_EXT, OFFLINE_RE, RATE_RE, Paths, Settings
from .destinations import destination_for, filehost_folder
from .engines import Engines, dir_signature, probe_down, produced, run
from .mega import folder_name as mega_folder_name
from .sites import SiteRule
from .urls import Knowledge, Playlist, host, resolve_short_url

_PATH_CHARS = r'[^\r\n"<>|]+'
_SKIP_LINE = re.compile(r"(?m)^#\s+(.+?)\s*$")
# yt-dlp prints this when the file is still on disk and it refuses to fetch it
# again. With no download archive that is the normal way a repeat is skipped, and
# it is a success: the bytes are there.
_ALREADY_LINE = re.compile(r"(?m)^\[download\]\s+.+ has already been downloaded")
# The kemono family stores a post's files even when only the thumbnail was
# archived; gallery-dl refuses to save that thumbnail as the real file and says
# so. Nothing will ever fetch the original, so this is not a failure.
_PREVIEW_LINE = re.compile(r"\('preview only'\)")
# yt-dlp's own wording when the account may not watch a video ("Private video.
# Login if you have permissions to watch."). Retrying cannot change it.
_PRIVATE_LINE = re.compile(r"(?i)\bprivate video\b")
# The kemono family's warning for a post it knows but has not imported yet:
# every file URL answers 404 until the mirror does the import.
_PENDING_LINE = re.compile(r"(?i)incomplete/missing file import")

# Conditions an engine retries forever instead of failing. megatools sleeps for
# minutes per chunk on a MEGA "509 over quota", so the lane would stay busy
# until the quota resets; kill it and let the URL go to fail.txt instead.
_QUOTA_STOP = (
    r"(?i)(?:server returned 509|over ?quota|transfer quota|quota exceeded"
    r"|EBLOCKED)"
)

# A Drive folder's own name, for the output folder instead of its id. The
# public folder page carries the name in its <title> ("加长HMV - Google Drive");
# one request per folder is cheap next to the download. A failed lookup returns
# "" so the caller falls back to the id, which is where gdown would put it.
_DRIVE_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_BAD_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def drive_folder_name(folder_id: str, timeout: float = 15.0) -> str:
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            page = response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a name is optional, never fatal
        return ""
    match = _DRIVE_TITLE.search(page)
    if not match:
        return ""
    name = html_lib.unescape(match.group(1)).strip()
    name = re.sub(r"\s*-\s*Google Drive$", "", name, flags=re.IGNORECASE)
    name = _BAD_FOLDER_CHARS.sub("_", name).strip().rstrip(".")
    return name[:120]


def drive_file_gone(url: str, timeout: float = 15.0) -> bool:
    """True when Drive's download endpoint answers 404 for this file.

    gdown reports "Cannot retrieve the public link ... or have had many
    accesses" for both a deleted file and a temporarily throttled one, so the
    status is asked directly: a live file answers 206 to a one-byte range (and
    the connection is dropped unread), a deleted one answers 404.
    """
    match = re.search(r"/file/d/([A-Za-z0-9_-]+)", url)
    if not match:
        return False
    probe = f"https://drive.google.com/uc?id={match.group(1)}&export=download"
    request = urllib.request.Request(probe, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Range": "bytes=0-0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 404
    except urllib.error.HTTPError as exc:
        return exc.code == 404
    except Exception:  # noqa: BLE001 - a probe must never break the download
        return False


def page_from_fragment(url: str) -> int | None:
    """The page the ppixiv userscript records in the URL fragment.

    ``…#ppixiv?page=5`` means the fifth picture, so the number is used as-is for
    gallery-dl's 1-based ``--range``. Nothing else in the URL uses the fragment.
    """
    fragment = url.partition("#")[2]
    if not fragment:
        return None
    match = re.search(r"[?&]page=(\d+)", fragment)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class FileRecord:
    name: str
    path: Path
    size: int


@dataclass
class GalleryResult:
    engine: str | None = None
    page_ids: list[str] = field(default_factory=list)
    pages_manifest: Path | None = None
    handled: bool = False
    auth: str = ""
    preview: bool = False
    pending: bool = False


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _post_id(url: str) -> str:
    """The last path segment of a post URL - what the folder is named after."""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1]


def _safe_name(name: str) -> str:
    """A filename fragment that every filesystem accepts."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    return re.sub(r"\s+", " ", cleaned).strip()[:120].strip()


def _description_text(data: dict, url: str) -> str:
    """A plain-text ``.description`` from gallery-dl's post metadata."""
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "")
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</p\s*>", "\n\n", content)
    content = re.sub(r"<[^>]+>", "", content)
    content = html_lib.unescape(content)
    content = re.sub(r"[ \t]+\n", "\n", content)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    head = "\n".join(
        part for part in (title, url, str(data.get("published") or "")) if part
    )
    return f"{head}\n\n{content}\n" if content else f"{head}\n"


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def file_records(
    text: str,
    dest: str | Path,
    manifest: str | Path | None = None,
    *,
    include_skipped: bool = False,
    extensions: str | None = None,
) -> list[FileRecord]:
    """Media files an engine wrote, with their on-disk sizes.

    The manifest wins over parsed text: both frozen exes encode stdout in the
    ANSI codepage and drop characters it cannot represent, so the printed path
    stops matching the file on disk. ``--print-to-file`` writes the real path in
    UTF-8. Without a manifest the text is parsed and every path is checked on
    disk, which also drops merged-away intermediates.

    ``extensions`` restricts what counts as a download (``a|b|c`` without the
    dot). A manifest from the generic tier happily lists ``adv_vert.js`` or a
    ``.css`` file; those are page assets, not media.
    """
    allowed = extensions or MEDIA_EXT
    ext_re = re.compile(rf"(?i)\.(?:{allowed})$")
    dest_text = str(dest)
    paths: list[str] = []
    have_manifest = False
    if manifest:
        mp = Path(manifest)
        try:
            have_manifest = mp.is_file() and mp.stat().st_size > 0
        except OSError:
            have_manifest = False
    if have_manifest:
        for line in Path(manifest).read_text(encoding="utf-8", errors="replace").splitlines():
            path = line.strip()
            if path and path.lower().startswith(dest_text.lower()) and ext_re.search(path):
                paths.append(path)
    else:
        body = text
        if not include_skipped:
            body = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
        pattern = re.compile(
            r"(?i)(" + re.escape(dest_text) + _PATH_CHARS + rf"\.(?:{allowed})"
            + r")(?=$|[\r\n\"<>|]|\s)"
        )
        paths = [m.group(1) for m in pattern.finditer(body)]

    records: list[FileRecord] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        p = Path(path)
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        records.append(FileRecord(name=p.name, path=p, size=size))
    return records


def unique_path(path: str | Path) -> Path:
    """``name (1).ext`` for the first free name next to ``path``."""
    p = Path(path)
    if not p.exists():
        return p
    for i in range(1, 1000):
        candidate = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not candidate.exists():
            return candidate
    return p


class Worker:
    """Runs the tier chain for exactly one URL."""

    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        engines: Engines,
        knowledge: Knowledge,
        *,
        filehosts: tuple[str, ...] = (),
        shorteners: tuple[str, ...] = (),
        strict_generic: bool | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.engines = engines
        self.knowledge = knowledge
        self.filehosts = filehosts
        self.shorteners = shorteners
        self.strict = (
            settings.strict_generic if strict_generic is None else strict_generic
        )
        self._buffer: list[str] = []
        self._log_path: Path | None = None
        self._lock = threading.Lock()
        self._url = ""
        self._temps: list[Path] = []
        self._drive_names: dict[str, str] = {}
        self._quota_host = ""
        self._gone = False
        self._invalid = False
        self._extractor = ""

    # -- logging ---------------------------------------------------------
    def _engine_run(self, args, **kwargs):
        """``run()`` for an engine child, with the stall watchdog applied.

        Every engine call goes through here: an engine that prints nothing for
        ``stall_seconds`` is killed, so a silent hang (megatools waiting on a
        blocked IP, a dead socket) cannot hold a lane forever.
        """
        kwargs.setdefault("cwd", self.paths.root)
        kwargs.setdefault("stall", self.settings.stall_seconds)
        return run(args, **kwargs)

    def _lane(self, message: str) -> None:
        line = f"[{_clock()}] {message}"
        with self._lock:
            self._buffer.append(line)
            if self._log_path:
                try:
                    with self._log_path.open("a", encoding="utf-8", newline="") as fh:
                        fh.write(line + "\r\n")
                except OSError:
                    pass

    def _sink(self, line: str) -> None:
        with self._lock:
            self._buffer.append(line)
            if self._log_path:
                try:
                    with self._log_path.open("a", encoding="utf-8", newline="") as fh:
                        fh.write(line + "\r\n")
                except OSError:
                    pass

    @property
    def text(self) -> str:
        return "\n".join(self._buffer)

    # -- entry point -----------------------------------------------------
    def download(
        self,
        url: str,
        *,
        dest: str | Path | None = None,
        flat: bool = False,
        dest_tpl: str = "",
        ignore_records: bool = False,
        refresh: bool = False,
        lane_log: str | Path | None = None,
        result: str | Path | None = None,
        archive: str | Path | None = None,
        slot: int = 0,
        queue_spec: str = "",
    ) -> str:
        self._buffer = []
        self._url = url
        self.paths.ensure()
        self._log_path = Path(lane_log) if lane_log else self.paths.lane_log(slot)
        try:
            self._log_path.write_text("", encoding="utf-8")
        except OSError:
            self._log_path = None

        resolved = resolve_short_url(url, self.shorteners)
        if resolved != url:
            self._lane(f"[redirect] {url} -> {resolved}")
        self._quota_host = ""      # set when the engine says the IP is out of quota
        self._gone = False         # set when a probe proves the file is deleted
        self._invalid = False      # set when the link itself cannot be parsed

        if dest is None:
            target = destination_for(resolved, self.knowledge, self.settings)
            dest_root = target.path
            flat = target.layout == "flat"
            dest_tpl = target.tpl
        else:
            dest_root = Path(dest)
        folder = filehost_folder(
            resolved, self.knowledge, self.filehosts,
            mega_name=mega_folder_name(resolved),
        )
        final_dest = dest_root / folder if folder else dest_root
        if folder:
            self._lane(f"[filehost] writing into {folder}\\")

        dl = self.knowledge.dl_url(resolved)
        if dl != resolved:
            from .urls import host as url_host

            if url_host(dl) != url_host(resolved):
                self._lane(f"[mirror] {url_host(resolved)} -> {url_host(dl)}")
            else:
                self._lane(f"[url] tracking stripped: {dl}")
        final_dest.mkdir(parents=True, exist_ok=True)
        # Published so the pass can link the lane line at the folder this URL
        # writes into: the same folder the worker just resolved, file-host
        # collection folder included.
        try:
            self.paths.lane_dest(slot).write_text(str(final_dest), encoding="utf-8")
        except OSError:
            pass

        dry = self.settings.dry_run
        sim = ["--simulate"] if dry else []
        ignore = bool(ignore_records) or self.settings.ignore_records
        ignore_args = ["--no-download-archive"] if ignore else []
        refresh_args = ["--force-overwrites"] if refresh else []

        rule = self.knowledge.rule(resolved)
        dl_rule = self.knowledge.rule(dl) or rule
        site_yt = list((dl_rule or rule).yt) if (dl_rule or rule) else []

        if not dry and probe_down(dl_rule, self.paths):
            self._lane(
                f"[skip] {dl_rule.key or dl_rule.host}: no content server answering "
                f"({', '.join(dl_rule.probe)}) - retry later"
            )
            return self._finish("FAILED", result)

        # A creator page yt-dlp can list flat is expanded, not crawled: the
        # listing returns item URLs in seconds and the next pass drops the ones
        # already in downloads.tsv.
        if dl_rule and dl_rule.list and not dry and self.engines.yt:
            path = _url_path(dl)
            if path and re.search(dl_rule.list, path):
                verdict = self._expand_flat(dl, queue_spec)
                if verdict:
                    return self._finish(verdict, result)

        playlist = self.knowledge.playlist(resolved)
        page_ids: list[str] = []
        gdl_handled = False
        engine: str | None = None
        sig0 = dir_signature(final_dest)
        files_manifest = (
            Path(str(self._log_path).rsplit(".log", 1)[0] + ".files")
            if self._log_path
            else self.paths.lane_files(slot)
        )
        try:
            files_manifest.unlink()
        except OSError:
            pass
        extractor_file = self.paths.lane_extractor(slot)
        extractor_args = ["--print-to-file", "after_move:extractor", str(extractor_file)]
        try:
            extractor_file.unlink()
        except OSError:
            pass
        # The path the engine is about to write, published for the lane link.
        target_file = self.paths.lane_target(slot)
        try:
            target_file.unlink()
        except OSError:
            pass
        blocked = False

        # engine= in sites.txt names one engine for the host and nothing else:
        # a failed download is retried on a later run instead of being handed to
        # a chain of engines that can only repeat the same answer (an iwara CDN
        # timeout became yt-dlp -> yt-dlp-native -> yt-dlp-generic -> gallery-dl
        # for every file). Only hosts with no pin cascade.
        pinned = (dl_rule.engine if dl_rule else "") or ""
        pin_yt = pinned in ("yt-dlp", "ytdlp")
        pin_gdl = pinned in ("gdl", "gallery-dl")
        pin_filehost = bool(pinned) and not pin_yt and not pin_gdl

        # --- tier 1: gallery-dl -----------------------------------------
        if self.engines.gdl and (not pinned or pin_gdl):
            gallery = self._gallery_tier(
                resolved, dl, final_dest, flat, folder, dl_rule, playlist,
                files_manifest, sim, ignore, archive, sig0, dry, target_file,
            )
            engine = gallery.engine
            page_ids = gallery.page_ids
            gdl_handled = gallery.handled
            if gallery.auth:
                return self._finish(f"AUTH|{gallery.auth}", result)
            if gallery.preview:
                return self._finish("PREVIEW|gallery-dl", result)
            if gallery.pending:
                return self._finish("PENDING|gallery-dl", result)

        # --- tier 1f: file-host engines ---------------------------------
        if not engine and not dry and pin_filehost:
            engine = self._filehost_engine(dl, final_dest, dl_rule, files_manifest)
            if engine is None and self._quota_host:
                # Host-wide: the quota is per IP, so one answer condemns them all.
                return self._finish(f"QUOTA|{self._quota_host}", result)

        # --- tiers 2-3: yt-dlp ------------------------------------------
        # dest_tpl wins over the site rule: it is either the destination's own
        # template or a full path template built from a queue line's
        # destination, and in both cases the caller was explicit about it.
        if dest_tpl:
            tpl = dest_tpl
        elif rule and rule.tpl:
            tpl = rule.tpl
        elif flat:
            tpl = self.settings.tpl_flat
        elif folder:
            tpl = self.settings.tpl_generic
        else:
            tpl = self.settings.tpl_default
        common = ["-P", str(final_dest), "-o", tpl]
        if flat or folder or dest_tpl:
            common3 = common
        elif rule and rule.tpl:
            common3 = ["-P", str(final_dest), "-o", rule.tpl]
        else:
            common3 = ["-P", str(final_dest), "-o", self.settings.tpl_generic]
        manifest_args = ["--print-to-file", "after_move:filepath", str(files_manifest)]
        # Before the bytes start, yt-dlp knows the final path (outtmpl applied),
        # so the lane line can link at the folder the file is really going into -
        # the creator subfolder included - instead of the destination root.
        target_args = (
            ["--print-to-file", "before_dl:%(filename)s", str(target_file)]
            if not dry else []
        )
        progress_args = [
            "--newline", "--progress", "--progress-template",
            ("download:dredge-yt %(progress.downloaded_bytes)s "
             "%(progress.total_bytes)s %(progress.speed)s"),
        ]
        yt_log = ""

        if not engine and not gdl_handled and self.engines.yt and (
            not pinned or pin_yt
        ):
            self._lane(f"[yt-dlp] {resolved}")
            yt_args: list[str] = []
            if dl_rule and dl_rule.native:
                yt_args = ["--downloader", "native"]
                self._lane("[yt-dlp] native downloader (native in sites.txt)")
            mark = len(self._buffer)
            rc, _ = self._engine_run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_conf), *yt_args, *site_yt, *ignore_args,
                 *refresh_args, *common, *manifest_args, *target_args, *progress_args,
                 *extractor_args, *sim, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and (
                produced(final_dest, sig0, self.text, dry)
                or _ALREADY_LINE.search(self.text)
            ):
                engine = "yt-dlp"
                self._extractor = self._read_extractor(extractor_file)
                if self._strict_blocks(resolved, self._extractor):
                    engine = None
                    blocked = True
            yt_log = "\n".join(self._buffer[mark:])

        if (
            not engine and not blocked and not gdl_handled and not dry and yt_log
            and not pinned
            and not re.search(r"Unsupported URL|is not a valid URL", yt_log)
            and self.engines.yt
        ):
            self._lane(f"[yt-dlp-native] {resolved} (retry without aria2c)")
            sig1 = dir_signature(final_dest)
            rc, _ = self._engine_run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_conf), "--downloader", "native", *site_yt,
                 *ignore_args, *refresh_args, *common, *manifest_args, *target_args,
                 *progress_args, *extractor_args, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and (
                produced(final_dest, sig1, self.text, False)
                or _ALREADY_LINE.search(self.text)
            ):
                engine = "yt-dlp-native"
                self._extractor = self._read_extractor(extractor_file)
                if self._strict_blocks(resolved, self._extractor):
                    engine = None
                    blocked = True

        generic_allowed = True
        if self.strict and not gdl_handled and not pinned:
            from .urls import host as url_host

            h = url_host(resolved)
            generic_allowed = any(
                h == d or h.endswith("." + d) for d in self._generic_sites()
            )
            if not generic_allowed:
                self._lane(
                    f"[strict] {h} is not in generic-sites.txt - the generic tier is "
                    "skipped (add the host there to allow it)"
                )

        if (
            not engine and not blocked and not gdl_handled and generic_allowed
            and self.engines.yt and not pinned
        ):
            self._lane(f"[yt-dlp-generic] {resolved}")
            rc, _ = self._engine_run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_generic_conf), *ignore_args, *refresh_args,
                 *site_yt, *common3, *manifest_args, *target_args, *progress_args,
                 *extractor_args, *sim, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and (
                produced(final_dest, sig0, self.text, dry)
                or _ALREADY_LINE.search(self.text)
            ):
                engine = "yt-dlp-generic"
                self._extractor = self._read_extractor(extractor_file)

        # A yt-dlp tier counts only if a media file is actually on disk: a
        # manifest can list page assets (.js, .css) or preview images, which
        # are not downloads. Small videos are fine - size is not a signal.
        if (
            engine and str(engine).startswith("yt-dlp") and not dry
            and not file_records(self.text, final_dest, files_manifest, extensions=AV_EXT)
        ):
            engine = None
            self._lane(
                "[yt-dlp] no media file on disk after a successful exit - counting as failed "
                "(a page asset or an image is not a download)"
            )

        if engine:
            verdict = f"OK|{engine}"
        elif RATE_RE.search(self.text):
            verdict = "FAILED"          # throttled: retry on a later run
        elif _PRIVATE_LINE.search(self.text):
            # yt-dlp says "Private video. Login if you have permissions to
            # watch." - the account cannot see it, and a retry will not change
            # that, so the pass parks it instead of adding it to fail.txt.
            verdict = "PRIVATE|yt-dlp"
        elif self._invalid:
            # The link itself is unusable (a MEGA folder without its #key), so
            # there is nothing to retry; the pass parks it in skip.txt.
            verdict = "INVALID|megadl"
        elif self._gone or OFFLINE_RE.search(self.text):
            verdict = "OFFLINE"
        else:
            verdict = "FAILED"

        partial = bool(playlist and not engine and page_ids)
        if (engine or partial) and not dry and archive:
            self._write_rows(
                archive, url, engine, final_dest, files_manifest, rule,
                playlist, page_ids,
            )
        return self._finish(verdict, result)

    # -- helpers ---------------------------------------------------------
    def _finish(self, verdict: str, result: str | Path | None) -> str:
        self._lane(f"verdict: {verdict}")
        for temp in self._temps:
            try:
                temp.unlink()
            except OSError:
                pass
        self._temps.clear()
        if result:
            try:
                Path(result).write_text(verdict, encoding="utf-8")
            except OSError:
                pass
        return verdict

    def _generic_sites(self) -> tuple[str, ...]:
        from .destinations import load_domains

        return load_domains(self.paths.generic_sites)

    def _gallery_tier(
        self,
        url: str,
        dl: str,
        final_dest: Path,
        flat: bool,
        folder: str,
        dl_rule: SiteRule | None,
        playlist: Playlist | None,
        files_manifest: Path,
        sim: list[str],
        ignore: bool,
        archive: str | Path | None,
        sig: str,
        dry: bool,
        target_file: Path | None = None,
    ) -> GalleryResult:
        """One gallery-dl attempt; used first or as the yt-dlp fallback."""
        result = GalleryResult()
        self._lane(f"[gallery-dl] {url}")
        args = self._gdl_args(
            dl, final_dest, flat or bool(folder), dl_rule, playlist,
            files_manifest, sim, ignore, target_file,
        )
        page = page_from_fragment(url)
        if page is None and dl_rule and dl_rule.page.isdigit():
            # The rule decides what a link without a page means (pixiv: page 1,
            # the first picture, instead of the whole post).
            page = int(dl_rule.page)
        if page is not None:
            page = max(1, page)
            self._lane(f"[gallery-dl] page {page} only")
            args = ["--range", str(page), *args]
        if playlist and archive:
            result.pages_manifest = Path(str(archive) + ".pages")
            try:
                result.pages_manifest.unlink()
            except OSError:
                pass
            args = ["--Print-to-file", "after:{id}", str(result.pages_manifest)] + args

        rc, _ = self._engine_run([self.engines.gdl, *args], cwd=self.paths.root, sink=self._sink)
        if ignore:
            self._auto_rename(args, files_manifest)
        out = self.text
        if dl_rule and dl_rule.oauth and re.search(
            r"(?i)AuthenticationError|refresh.?token'?.? required", out
        ):
            # The site is pinned to gallery-dl and it has no token yet. The pass
            # runs the login flow once and retries the link, so a fresh install
            # authorises itself instead of failing every link it sees.
            self._lane(
                f"[gallery-dl] {dl_rule.oauth} needs authorization - "
                "the pass will run its login flow"
            )
            result.auth = dl_rule.oauth
            return result
        unsupported = bool(re.search(r"Unsupported URL|is not a valid URL", out))
        page_err = bool(re.search(r"\[(?!download\b)[\w-]+\]\[error\]", out))
        file_err = bool(re.search(r"\[download\]\[error\]", out))
        n_file_err = len(re.findall(r"\[download\]\[error\]", out))
        n_404 = len(re.findall(r"404 Not Found", out))
        throttled = bool(re.search(r"(?i)(too many requests|rate limit|too many attempts)", out))
        dead_only = bool(
            playlist and n_file_err and not page_err and n_404 >= n_file_err and not throttled
        )
        gdl_err = (file_err or page_err) and not unsupported and not dead_only
        result.page_ids = self._page_ids(result.pages_manifest) if playlist else []
        exit_ok = rc == 0 or dead_only
        existing = file_records(out, final_dest, files_manifest, include_skipped=True)
        made = produced(final_dest, sig, out, dry)
        skipped = bool(_SKIP_LINE.search(out))
        if exit_ok and not gdl_err and (
            result.page_ids or made or existing or skipped
        ):
            result.engine = "gallery-dl"
            if not made and existing:
                self._lane(
                    "[gallery-dl] files already on disk - recording them as downloaded"
                )
            elif not made and skipped:
                self._lane(
                    "[gallery-dl] archived as downloaded - recording the post, "
                    "not fetching it again"
                )
        if not result.engine and playlist and not unsupported:
            result.handled = True
        if dl_rule and dl_rule.engine == "gdl":
            result.handled = True

        if _PENDING_LINE.search(out) and not existing and not playlist:
            # "Incomplete/Missing file import ('pending')": the archive knows the
            # post's files but has not imported them, so every URL it lists
            # answers 404. Nothing can be fetched now, and the post text is the
            # only thing worth keeping, so the link is parked like a preview.
            self._lane(
                "[gallery-dl] the archive has not imported this post yet - "
                "parking the link"
            )
            if self._save_parked_text():
                self._save_description(final_dest, dl, args)
            result.pending = True
            result.handled = True
        elif (
            exit_ok and not gdl_err and not playlist
            and _PREVIEW_LINE.search(out) and not existing
        ):
            # The archive knows the post's files but kept only thumbnails:
            # gallery-dl will not save a thumbnail under the real filename, and
            # no other engine has the original either. Not a failure - the pass
            # parks the link, but this must not look like a dead post or be
            # retried forever.
            self._lane(
                "[gallery-dl] the archive has previews only - parking the link"
            )
            if self._save_parked_text():
                self._save_description(final_dest, dl, args)
            result.preview = True
            result.handled = True
        return result

    def _save_parked_text(self) -> bool:
        """Whether a parked post (preview-only or pending) keeps its text."""
        return self.settings.preview_description.strip().lower() not in (
            "off", "no", "false", "0", "",
        )

    def _save_description(self, dest: Path, url: str, args: list[str]) -> None:
        """Save the post text where gallery-dl would have put the media.

        ``--write-info-json`` writes the post metadata even when every file is
        skipped, into the same folder the files would have gone to. The
        ``.description`` beside it is the same plain-text sidecar yt-dlp writes
        for a video, so a preview-only post still leaves a record of what it is.
        """
        if self.settings.dry_run:
            return
        self._engine_run(
            [self.engines.gdl, "--write-info-json", *args],
            cwd=self.paths.root, sink=self._sink,
        )
        # No exit-code check: a pending import fails the same 404s again but
        # still writes info.json, which is the only thing wanted here.
        info = self._find_info(dest, _post_id(url))
        if info is None:
            self._lane("[gallery-dl] no info.json was written - no post text to keep")
            return
        try:
            data = json.loads(info.read_text(encoding="utf-8", errors="replace"))
            stem = _safe_name(f"{data.get('id') or info.parent.name}_{data.get('title') or ''}")
            target = info.parent / f"{stem or 'post'}.description"
            target.write_text(_description_text(data, url), encoding="utf-8")
        except (OSError, ValueError):
            return

    @staticmethod
    def _find_info(dest: Path, post_id: str) -> Path | None:
        """The info.json gallery-dl wrote for this post, if it is findable."""
        if not post_id:
            return None
        for pattern in (f"{post_id}/info.json", f"*/{post_id}/info.json",
                        f"*/*/{post_id}/info.json"):
            for path in dest.glob(pattern):
                if path.is_file():
                    return path
        return None

    def _ytdlp_author(self, url: str) -> str:
        """The uploader's display name from yt-dlp (gallery-dl often has the handle)."""
        if not self.engines.yt:
            return ""
        _, out = self._engine_run(
            [self.engines.yt, "--ignore-config", "--config-locations",
             str(self.paths.ytdlp_conf), "--no-download-archive", "--simulate",
             "--no-warnings", "--print", "%(uploader)s", url],
            cwd=self.paths.root,
        )
        for line in reversed(out.splitlines()):
            name = line.strip()
            if name and not name.startswith("[") and name != "NA":
                return name
        return ""

    def _gdl_args(
        self,
        dl: str,
        dest: Path,
        flat: bool,
        rule: SiteRule | None,
        playlist: Playlist | None,
        files_manifest: Path,
        sim: list[str],
        ignore: bool,
        target_file: Path | None = None,
    ) -> list[str]:
        args = ["--config-ignore", "-c", str(self.paths.gallery_conf)]
        if self.paths.gallery_runtime.exists():
            args += ["-c", str(self.paths.gallery_runtime)]
        args += ["-D" if flat else "-d", str(dest)]
        if rule and rule.gdl:
            for option in rule.gdl:
                args += ["-o", option]
        if rule and rule.author == "yt-dlp":
            author = self._ytdlp_author(dl)
            if author:
                args += ["-o", f"directory={author}"]
        if files_manifest:
            args += ["--Print-to-file", "after:{_path}", str(files_manifest)]
        if target_file and not sim:
            # prepare-after: the target path exists but nothing was fetched yet
            args += [
                "--Print-to-file", "prepare-after:{_path}", str(target_file),
            ]
        if ignore:
            temp = Path(_temp_archive())
            self._temps.append(temp)
            args = ["--download-archive", str(temp)] + args
        args += sim
        args.append(dl)
        return args

    def _auto_rename(self, args: list[str], files_manifest: Path) -> None:
        """Move files gallery-dl skipped to a free name, then fetch new copies."""
        aside: list[Path] = []
        for m in _SKIP_LINE.finditer(self.text):
            p = Path(m.group(1).strip())
            if p.is_file():
                aside.append(p)
        aside = list(dict.fromkeys(aside))
        if not aside:
            return
        moved = 0
        for p in aside:
            try:
                p.rename(unique_path(p))
                moved += 1
            except OSError:
                pass
        self._lane(f"[auto-rename] {moved} existing file(s) moved aside")
        self._engine_run([self.engines.gdl, "--no-skip", *args], cwd=self.paths.root, sink=self._sink)

    def _page_ids(self, manifest: Path | None) -> list[str]:
        if not manifest or not manifest.is_file():
            return []
        out = []
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if re.fullmatch(r"\d+", text):
                out.append(text)
        return list(dict.fromkeys(out))

    def _read_extractor(self, path: Path) -> str:
        """The extractor yt-dlp reported for the files it moved (last line)."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1].lower() if lines else ""

    def _strict_blocks(self, url: str, extractor: str) -> bool:
        """True when strict mode refuses a generic result from an unknown host."""
        if not self.strict or extractor != "generic":
            return False
        from .urls import host as url_host

        host = url_host(url)
        if any(host == d or host.endswith("." + d) for d in self._generic_sites()):
            return False
        self._lane(
            f"[strict] {host} has no dedicated extractor - add `{host}` to "
            "config/generic-sites.txt to allow the generic tier"
        )
        return True

    def _expand_flat(self, dl: str, spec: str = "") -> str | None:
        self._lane(f"[expand] flat listing {dl}")
        _rc, out = self._engine_run(
            [self.engines.yt, "--ignore-config", "--config-locations",
             str(self.paths.ytdlp_conf), "--no-download-archive", "--flat-playlist",
             "--simulate", "--print", "%(webpage_url)s", dl],
            cwd=self.paths.root,
        )
        items = list(dict.fromkeys(
            line.strip() for line in out.splitlines()
            if re.match(r"(?i)^https?://", line.strip())
        ))
        if not items:
            self._lane("[expand] yt-dlp listed nothing - crawling the page instead")
            return None
        staged = self.paths.staged_queue
        # The line's destination travels with every item: a creator URL copied
        # with a folder still means that folder for each of its posts.
        payload = "".join(
            (f"{item}\t{spec}" if spec else item) + "\r\n" for item in items
        )
        for _ in range(5):
            try:
                with staged.open("a", encoding="utf-8", newline="") as fh:
                    fh.write(payload)
                self._lane(f"[expand] {len(items)} item URL(s) staged for the next pass")
                return "OK|expanded"
            except OSError:
                time.sleep(0.2)
        self._lane("[expand] could not write queue-requeue.txt - crawling the page instead")
        return None

    def _filehost_engine(
        self,
        dl: str,
        dest: Path,
        rule: SiteRule,
        files_manifest: Path,
    ) -> str | None:
        name = rule.engine
        exe = self.engines.for_rule(name)
        if not exe:
            self._lane(f"[{name}] not installed - cannot fetch this host without it (engine= in sites.txt)")
            return None
        self._lane(f"[{name}] {dl}")
        # Download straight into the destination: megatools keeps its partial
        # data in .megatmp.<id> files next to the target, so a killed run can
        # resume instead of starting over. Nothing is deleted on failure.
        before = _file_snapshot(dest)
        if name == "megadl":
            args = [exe, "dl", "--path", str(dest), "--no-ask-password",
                    "--ignore-config-file", dl]
        elif name == "gdown":
            # Drive is used for public folders. Asking as the logged-in account
            # turns "public but not shared with me" into an empty listing, so
            # anonymous is the default and the jar is only a fallback.
            args = self._gdown_args(exe, dl, dest, cookies=False)
        else:
            args = [exe, "-o", str(dest), "-m", "4", "-t", "2", dl]
        rc, out = self._engine_run(args, cwd=self.paths.root, sink=self._sink, stop_on=_QUOTA_STOP)
        moved = sorted(_file_snapshot(dest) - before)
        if name == "gdown" and "--no-cookies" in args and (
            rc != 0 or ("/folders/" in dl and not moved)
        ) and self.paths.cookies.exists():
            # Private links are the only thing the jar helps with: an anonymous
            # run either errors or lists nothing. Retrying costs one request.
            self._lane(
                "[gdown] anonymous access did not work - retrying with the cookie jar"
            )
            rc, out = self._engine_run(
                self._gdown_args(exe, dl, dest, cookies=True),
                cwd=self.paths.root, sink=self._sink, stop_on=_QUOTA_STOP,
            )
            moved = sorted(_file_snapshot(dest) - before)
        if moved:
            try:
                files_manifest.write_text(
                    "".join(path + "\n" for path in moved), encoding="utf-8"
                )
            except OSError:
                pass
        if name == "megadl":
            if re.search(r"(?i)invalid mega download link", out):
                # megatools refuses the URL before it ever talks to MEGA: a
                # folder link needs its "#<key>", so nothing can be known about
                # the folder - not even whether it still exists. Park it.
                self._invalid = True
                self._lane(
                    "[megadl] the link is incomplete (a MEGA folder link needs "
                    "its #key) - parked; paste the full link and retry skipped"
                )
            elif re.search(r"(?i)malformed attributes", out):
                # The folder answered, but its attributes do not decrypt: the
                # key is wrong (a 22-character key from another link does this).
                # Retrying with the same link cannot help either.
                self._invalid = True
                self._lane(
                    "[megadl] the #key does not decrypt this folder - wrong key, "
                    "not a dead link; parked, retry skipped with the right link"
                )
        if rc == 0 and moved:
            return name
        if rc == 0:
            if not self._invalid:
                self._lane(f"[{name}] exit 0 but nothing new on disk - counting as failed")
        else:
            self._lane(f"[{name}] exit {rc}")
            if moved:
                self._lane(f"[{name}] kept {len(moved)} finished file(s)")
            if any(p.is_file() for p in dest.rglob(".megatmp.*")):
                self._lane(f"[{name}] partial data kept - the next run resumes it")
            if re.search(r"(?i)over ?quota|transfer quota|quota exceeded", out):
                self._lane(f"[{name}] MEGA transfer quota for this IP is spent - retry on a later run")
            if re.search(r"(?i)EBLOCKED|IP (?:is )?blocked", out):
                # megatools answers EBLOCKED by sleeping for minutes per chunk,
                # so the lane hangs instead of failing. _QUOTA_STOP kills it on
                # sight; say why, because the link is fine.
                self._lane(
                    f"[{name}] MEGA blocked this IP - the links are fine, retry "
                    "after the block lifts (hours, not minutes)"
                )
            if re.search(_QUOTA_STOP, out):
                # The quota is per IP, so this answer applies to every link on
                # the host, not just this one: the pass parks the rest.
                self._quota_host = host(dl)
            if name == "megadl" and re.search(r"(?i)server returned error ENOENT", out):
                # ENOENT from MEGA's 'g' call means the node is not there at
                # all: deleted, or the link is wrong. No retry can fix it.
                self._gone = True
                self._lane(
                    "[megadl] MEGA answers ENOENT - the file no longer exists "
                    "(deleted, or a broken link), not a quota problem"
                )
            if re.search(r"(?i)too many users have viewed|download quota|quota exceeded"
                r"|cannot retrieve the public link|many accesses",
                out,
            ):
                self._lane(
                    f"[{name}] Drive refused it: the link is not public any more, "
                    "or the access limit for this file is spent - retry later"
                )
                if name == "gdown" and drive_file_gone(dl):
                    # The same wording covers a deleted file and a throttled one;
                    # the endpoint itself says which, and only a deleted file is
                    # worth parking in offline.txt.
                    self._gone = True
                    self._lane(
                        "[gdown] Drive answers 404 for this file - it was deleted, "
                        "not throttled"
                    )
        return None

    def _gdown_args(
        self, exe: str, url: str, dest: Path, *, cookies: bool
    ) -> list[str]:
        """The gdown command line, with or without the browser cookie jar.

        ``-c`` resumes a partial file and skips a finished one; ``--retries``
        covers the transient 403s Drive answers under load.
        """
        args = [exe, "-c", "--retries", "3", "-O", self._gdown_output(url, dest)]
        if "/folders/" in url:
            # gdown only treats a URL as a folder when told to: without this a
            # /drive/u/0/folders/<id>?resourcekey=... link is taken for a file
            # and the page itself is downloaded.
            args.append("--folder")
        args += self._gdown_cookies() if cookies else ["--no-cookies"]
        args.append(url)
        return args

    def _gdown_output(self, url: str, dest: Path) -> str:
        """Where gdown writes: a folder link gets a folder with the folder's name.

        gdown would name it after the raw id, which says nothing; the folder's
        own name is what a person recognises.
        """
        match = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
        if not match and "folderview" in url:
            match = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
        if match:
            folder_id = match.group(1)
            if folder_id not in self._drive_names:
                self._drive_names[folder_id] = drive_folder_name(folder_id)
            return str(dest / (self._drive_names[folder_id] or folder_id))
        return str(dest) + os.sep      # trailing separator: keep the file's name

    def _gdown_cookies(self) -> list[str]:
        """A throwaway copy of config/cookies.txt; gdown rewrites the file it reads."""
        source = self.paths.cookies
        if not source.exists():
            return ["--no-cookies"]
        target = Path(tempfile.gettempdir()) / f"dredge-gdown-{uuid.uuid4().hex}.txt"
        try:
            target.write_bytes(source.read_bytes())
        except OSError:
            return ["--no-cookies"]
        self._temps.append(target)
        return ["--cookies", str(target)]

    def _write_rows(
        self,
        archive: str | Path,
        url: str,
        engine: str | None,
        dest: Path,
        manifest: Path,
        rule: SiteRule | None,
        playlist: Playlist | None,
        page_ids: list[str],
    ) -> None:
        include_skipped = engine == "gallery-dl" and not playlist
        extensions = AV_EXT if str(engine or "").startswith("yt-dlp") else MEDIA_EXT
        records = file_records(
            self.text, dest, manifest,
            include_skipped=include_skipped, extensions=extensions,
        )
        if rule and records:
            records = self._apply_site_rules(rule, records)
        lines: list[str] = []
        if playlist:
            by_post: dict[str, list[FileRecord]] = {}
            post_re = re.compile(playlist.rule.postid)
            for record in records:
                m = post_re.search(record.name)
                if m:
                    by_post.setdefault(m.group(1), []).append(record)
            for post_id in sorted(by_post):
                hit = by_post[post_id]
                page = playlist.rule.posturl
                page = page.replace("{host}", playlist.host).replace("{service}", playlist.service)
                page = page.replace("{user}", playlist.user).replace("{id}", post_id)
                lines.append(format_row(_row(self.knowledge.canon(page), hit)))
        elif records:
            lines.append(format_row(_row(self.knowledge.canon(url), records)))
        elif engine == "gallery-dl" and not playlist and _SKIP_LINE.search(self.text):
            # gallery-dl's archive says this URL was downloaded; the files may
            # be on the other drive after a destination fallback. Record the
            # URL without files so it is deduplicated from the queue, exactly
            # as the archive considers it a duplicate.
            lines.append(format_row(_row(self.knowledge.canon(url), [])))
        if lines:
            try:
                Path(archive).write_text(
                    "".join(line + "\r\n" for line in lines),
                    encoding="utf-8",
                    newline="",
                )
            except OSError:
                pass

    def _apply_site_rules(self, rule: SiteRule, records: list[FileRecord]) -> list[FileRecord]:
        drop: list[FileRecord] = []
        if rule.min_size > 0:
            drop.extend(r for r in records if r.size < rule.min_size)
        if rule.keep > 0 and len(records) > rule.keep:
            drop.extend(records[rule.keep:])
        for record in drop:
            self._lane(f"[site] discarding {record.size:,} B: {record.name}")
            try:
                record.path.unlink()
            except OSError:
                pass
        doomed = {id(record) for record in drop}
        return [r for r in records if id(r) not in doomed]


def _row(url: str, records: list[FileRecord]):
    from .archive import Row

    return Row(
        timestamp=_now(),
        url=url,
        names=tuple(r.name for r in records),
        sizes=tuple(r.size for r in records),
    )


def _url_path(url: str) -> str:
    m = re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*://)?[^/?#]+([^?#]*)", url)
    return m.group(1) if m else ""


def _temp_archive() -> str:
    return str(Path(tempfile.gettempdir()) / f"dredge-archive-{uuid.uuid4().hex}.txt")


def _file_snapshot(folder: Path) -> set[str]:
    """Files under ``folder``, ignoring megatools' in-progress temp files."""
    if not folder.is_dir():
        return set()
    found: set[str] = set()
    for item in folder.rglob("*"):
        if item.is_file() and not item.name.startswith(".megatmp."):
            found.add(str(item))
    return found
