"""One URL, the whole tier chain, one verdict.

The worker is deliberately engine-agnostic: gallery-dl first, then yt-dlp with
its dedicated extractors, then yt-dlp again with its own downloader, then the
all-extractor pass. The next tier only runs when the previous one exited non-zero
or produced nothing. A host never picks an engine; the only skip is
``engine=gdl`` in ``sites.txt`` and "gallery-dl knows this creator page".

Verdicts: ``OK|<engine>``, ``OK|expanded`` (a flat listing staged its item URLs
for the next pass), ``FAILED``, ``OFFLINE``.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .archive import format_row
from .config import MEDIA_EXT, OFFLINE_RE, RATE_RE, Paths, Settings
from .destinations import filehost_folder, is_porn
from .engines import Engines, dir_signature, probe_down, produced, run
from .sites import SiteRule
from .urls import Knowledge, Playlist

_PATH_CHARS = r'[^\r\n"<>|]+'
_EXT = rf"\.(?:{MEDIA_EXT})"
_SKIP_LINE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_POST_NAME = re.compile(r"^(\d+)_[^_]*_(\d+)_")


@dataclass(frozen=True)
class FileRecord:
    name: str
    path: Path
    size: int


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def file_records(
    text: str,
    dest: str | Path,
    manifest: str | Path | None = None,
    *,
    include_skipped: bool = False,
) -> list[FileRecord]:
    """Media files an engine wrote, with their on-disk sizes.

    The manifest wins over parsed text: both frozen exes encode stdout in the
    ANSI codepage and drop characters it cannot represent, so the printed path
    stops matching the file on disk. ``--print-to-file`` writes the real path in
    UTF-8. Without a manifest the text is parsed and every path is checked on
    disk, which also drops merged-away intermediates.
    """
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
            if path and path.lower().startswith(dest_text.lower()):
                paths.append(path)
    else:
        body = text
        if not include_skipped:
            body = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
        pattern = re.compile(
            r"(?i)(" + re.escape(dest_text) + _PATH_CHARS + _EXT + r")(?=$|[\r\n\"<>|]|\s)"
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


def drop_gallery_entries(db: str | Path, names: list[str]) -> int:
    """Remove gallery-dl archive entries for files deleted as junk."""
    path = Path(db)
    if not names or not path.is_file():
        return 0
    targets: set[str] = set()
    keys: set[tuple[str, str]] = set()
    for name in names:
        base = Path(name).name
        targets.add(base.lower())
        m = _POST_NAME.match(base)
        if m:
            keys.add((m.group(1), str(int(m.group(2)))))
    dropped = 0
    try:
        con = sqlite3.connect(str(path))
        try:
            rows = [row[0] for row in con.execute("SELECT entry FROM archive")]
            doomed = []
            for entry in rows:
                low = str(entry).lower()
                if any(low.endswith(t) for t in targets):
                    doomed.append(entry)
                    continue
                tail = str(entry).rsplit("_", 2)
                if len(tail) == 3 and (tail[1], tail[2]) in keys:
                    doomed.append(entry)
            if doomed:
                con.executemany("DELETE FROM archive WHERE entry = ?", [(d,) for d in doomed])
                con.commit()
                dropped = len(doomed)
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    return dropped


class Worker:
    """Runs the tier chain for exactly one URL."""

    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        engines: Engines,
        knowledge: Knowledge,
        *,
        domains: tuple[str, ...] = (),
        filehosts: tuple[str, ...] = (),
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.engines = engines
        self.knowledge = knowledge
        self.domains = domains
        self.filehosts = filehosts
        self._buffer: list[str] = []
        self._log_path: Path | None = None
        self._lock = threading.Lock()
        self._url = ""
        self._temps: list[Path] = []

    # -- logging ---------------------------------------------------------
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
        is_porn_dest: bool = False,
        ignore_records: bool = False,
        lane_log: str | Path | None = None,
        result: str | Path | None = None,
        archive: str | Path | None = None,
        slot: int = 0,
    ) -> str:
        self._buffer = []
        self._url = url
        self.paths.ensure()
        self._log_path = Path(lane_log) if lane_log else self.paths.lane_log(slot)
        try:
            self._log_path.write_text("", encoding="utf-8")
        except OSError:
            self._log_path = None

        dest_root = Path(dest) if dest else (
            self.settings.dest_porn if is_porn(url, self.domains) else self.settings.dest_default
        )
        porn = bool(is_porn_dest) or dest_root == self.settings.dest_porn
        folder = filehost_folder(url, self.knowledge, self.filehosts)
        final_dest = dest_root / folder if folder else dest_root
        if folder:
            self._lane(f"[filehost] writing into {folder}\\")

        dl = self.knowledge.dl_url(url)
        if dl != url:
            from .urls import host as url_host

            if url_host(dl) != url_host(url):
                self._lane(f"[mirror] {url_host(url)} -> {url_host(dl)}")
            else:
                self._lane(f"[url] tracking stripped: {dl}")
        final_dest.mkdir(parents=True, exist_ok=True)

        dry = self.settings.dry_run
        sim = ["--simulate"] if dry else []
        ignore = bool(ignore_records) or self.settings.ignore_records
        ignore_args = ["--no-download-archive"] if ignore else []

        rule = self.knowledge.rule(url)
        dl_rule = self.knowledge.rule(dl) or rule

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
                verdict = self._expand_flat(dl)
                if verdict:
                    return self._finish(verdict, result)

        playlist = self.knowledge.playlist(url)
        pages_manifest: Path | None = None
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

        # --- tier 1: gallery-dl -----------------------------------------
        if self.engines.gdl:
            self._lane(f"[gallery-dl] {url}")
            args = self._gdl_args(
                dl, final_dest, porn or bool(folder), dl_rule, playlist,
                files_manifest, sim, ignore,
            )
            if playlist and archive:
                pages_manifest = Path(str(archive) + ".pages")
                try:
                    pages_manifest.unlink()
                except OSError:
                    pass
                args = ["--Print-to-file", "after:{id}", str(pages_manifest)] + args

            rc, _ = run([self.engines.gdl, *args], cwd=self.paths.root, sink=self._sink)
            if ignore:
                self._auto_rename(args, files_manifest)
            out = self.text
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
            page_ids = self._page_ids(pages_manifest) if playlist else []
            exit_ok = rc == 0 or dead_only
            if exit_ok and not gdl_err and (page_ids or produced(final_dest, sig0, out, dry)):
                engine = "gallery-dl"
            if engine == "gallery-dl" and not dry and self._remove_junk(out, final_dest, files_manifest):
                engine = None
                self._lane("[junk] gallery-dl produced only junk video files - counting as failed")

            if (
                engine == "gallery-dl" and not dry and not playlist
                and not file_records(out, final_dest, files_manifest, include_skipped=True)
            ):
                self._lane("[gallery-dl] only skip lines - retrying without its archive")
                retry_arc = Path(_temp_archive())
                self._temps.append(retry_arc)
                retry = [self.engines.gdl, "--download-archive", str(retry_arc), *args]
                self._buffer.clear()
                rc, _ = run(retry, cwd=self.paths.root, sink=self._sink)
                out = self.text
                if file_records(out, final_dest, files_manifest):
                    engine = "gallery-dl"
                    self._lane("[gallery-dl] archived file(s) were missing - downloaded them again")
                else:
                    engine = None
                    gdl_handled = True
                    self._lane(
                        "[gallery-dl] still only skip lines, nothing to record - counting as "
                        "failed (tools/adopt-files.py records files already on disk)"
                    )
            if not engine and playlist and not unsupported:
                gdl_handled = True
            if dl_rule and dl_rule.engine == "gdl":
                gdl_handled = True

        # --- tier 1f: file-host engines ---------------------------------
        if not engine and not dry and dl_rule and dl_rule.engine and dl_rule.engine != "gdl":
            engine = self._filehost_engine(dl, final_dest, dest_root, dl_rule, slot, files_manifest)

        # --- tiers 2-3: yt-dlp ------------------------------------------
        tpl = (
            self.settings.tpl_porn if porn
            else self.settings.tpl_generic if folder
            else rule.tpl if rule and rule.tpl
            else self.settings.tpl_default
        )
        common = ["-P", str(final_dest), "-o", tpl]
        common3 = (
            common if porn or folder
            else ["-P", str(final_dest), "-o", rule.tpl] if rule and rule.tpl
            else ["-P", str(final_dest), "-o", self.settings.tpl_generic]
        )
        manifest_args = ["--print-to-file", "after_move:filepath", str(files_manifest)]
        yt_log = ""

        if not engine and not gdl_handled and self.engines.yt:
            self._lane(f"[yt-dlp] {url}")
            yt_args: list[str] = []
            if dl_rule and dl_rule.native:
                yt_args = ["--downloader", "native"]
                self._lane("[yt-dlp] native downloader (native in sites.txt)")
            mark = len(self._buffer)
            rc, _ = run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_conf), *yt_args, *ignore_args, *common,
                 *manifest_args, *sim, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and produced(final_dest, sig0, self.text, dry):
                engine = "yt-dlp"
            yt_log = "\n".join(self._buffer[mark:])

        if (
            not engine and not gdl_handled and not dry and yt_log
            and not re.search(r"Unsupported URL|is not a valid URL", yt_log)
            and self.engines.yt
        ):
            self._lane(f"[yt-dlp-native] {url} (retry without aria2c)")
            sig1 = dir_signature(final_dest)
            rc, _ = run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_conf), "--downloader", "native", *ignore_args,
                 *common, *manifest_args, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and produced(final_dest, sig1, self.text, False):
                engine = "yt-dlp-native"

        generic_allowed = True
        if self.settings.strict_generic and not gdl_handled:
            from .urls import host as url_host

            h = url_host(url)
            generic_allowed = any(
                h == d or h.endswith("." + d) for d in self._generic_sites()
            )
            if not generic_allowed:
                self._lane(
                    f"[yt-dlp-generic] skipped: {h} is not in generic-sites.txt (-StrictGeneric)"
                )

        if not engine and not gdl_handled and generic_allowed and self.engines.yt:
            self._lane(f"[yt-dlp-generic] {url}")
            rc, _ = run(
                [self.engines.yt, "--ignore-config", "--config-locations",
                 str(self.paths.ytdlp_generic_conf), *ignore_args, *common3,
                 *manifest_args, *sim, dl],
                cwd=self.paths.root, sink=self._sink,
            )
            if rc == 0 and produced(final_dest, sig0, self.text, dry):
                engine = "yt-dlp-generic"

        if engine and not dry and self._remove_junk(
            self.text, final_dest, files_manifest if str(engine).startswith("yt-dlp") else None
        ):
            engine = None
            self._lane("[junk] only junk video files were produced - counting as failed")

        if (
            engine and str(engine).startswith("yt-dlp") and not dry
            and not file_records(self.text, final_dest, files_manifest)
        ):
            engine = None
            self._lane("[yt-dlp] no final file on disk after a successful exit - counting as failed")

        if engine:
            verdict = f"OK|{engine}"
        elif RATE_RE.search(self.text):
            verdict = "FAILED"          # throttled: retry on a later run
        elif OFFLINE_RE.search(self.text):
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
    ) -> list[str]:
        args = ["--config-ignore", "-c", str(self.paths.gallery_conf)]
        if self.paths.gallery_runtime.exists():
            args += ["-c", str(self.paths.gallery_runtime)]
        args += ["-D" if flat else "-d", str(dest)]
        if rule and rule.gdl:
            for option in rule.gdl:
                args += ["-o", option]
        if files_manifest:
            args += ["--Print-to-file", "after:{_path}", str(files_manifest)]
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
        run([self.engines.gdl, "--no-skip", *args], cwd=self.paths.root, sink=self._sink)

    def _page_ids(self, manifest: Path | None) -> list[str]:
        if not manifest or not manifest.is_file():
            return []
        out = []
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if re.fullmatch(r"\d+", text):
                out.append(text)
        return list(dict.fromkeys(out))

    def _remove_junk(self, text: str, dest: Path, manifest: Path | None) -> bool:
        present = [r for r in file_records(text, dest, manifest) if r.path.is_file()]
        if not present:
            return False
        junk = [
            r for r in present
            if re.search(r"(?i)\.(mp4|mkv|webm|mov|avi|flv|wmv|m4v|ts|mpg|mpeg|3gp)$", r.name)
            and r.size < self.settings.min_video_bytes
        ]
        if not junk:
            return False
        names = []
        for record in junk:
            self._lane(f"[junk] {record.size:,} B is not a video, deleting: {record.name}")
            try:
                record.path.unlink()
            except OSError:
                pass
            names.append(record.name)
            try:
                self.paths.junk_removed.parent.mkdir(parents=True, exist_ok=True)
                with self.paths.junk_removed.open("a", encoding="utf-8", newline="") as fh:
                    fh.write(
                        f"{_now()}\t{self.knowledge.canon(self._url)}\t"
                        f"{record.name}\t{record.size}\r\n"
                    )
            except OSError:
                pass
        dropped = drop_gallery_entries(self.paths.gallery_archive, names)
        if dropped:
            self._lane(f"[junk] archive entries dropped: {dropped}")
        return len(junk) >= len(present)

    def _expand_flat(self, dl: str) -> str | None:
        self._lane(f"[expand] flat listing {dl}")
        _rc, out = run(
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
        payload = "".join(item + "\r\n" for item in items)
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
        stage_root: Path,
        rule: SiteRule,
        slot: int,
        files_manifest: Path,
    ) -> str | None:
        name = rule.engine
        exe = self.engines.for_rule(name)
        if not exe:
            self._lane(f"[{name}] not installed - cannot fetch this host without it (engine= in sites.txt)")
            return None
        self._lane(f"[{name}] {dl}")
        stage = stage_root / f"~fh-lane{slot}"
        _reset_stage(stage)
        self._lane(f"[{name}] staging {stage}")
        moved: list[str] = []
        stop = threading.Event()

        def mover() -> None:
            while not stop.is_set():
                _move_stage(stage, dest, moved)
                stop.wait(2)

        thread: threading.Thread | None = None
        if name == "megadl":
            thread = threading.Thread(target=mover, daemon=True)
            thread.start()
        if name == "megadl":
            args = [exe, "dl", "--path", str(stage), "--no-ask-password",
                    "--ignore-config-file", dl]
        else:
            args = [exe, "-o", str(stage), "-m", "4", "-t", "2", dl]
        rc, out = run(args, cwd=self.paths.root, sink=self._sink)
        stop.set()
        if thread:
            thread.join(timeout=15)
        _move_stage(stage, dest, moved)
        moved = list(dict.fromkeys(moved))
        if moved:
            try:
                files_manifest.write_text(
                    "".join(path + "\n" for path in moved), encoding="utf-8"
                )
            except OSError:
                pass
        if rc == 0 and moved:
            return name
        if rc == 0:
            self._lane(f"[{name}] exit 0 but nothing new on disk - counting as failed")
        else:
            self._lane(f"[{name}] exit {rc}")
            if moved:
                self._lane(f"[{name}] kept {len(moved)} finished file(s)")
            if re.search(r"(?i)over ?quota|transfer quota|quota exceeded", out):
                self._lane(f"[{name}] MEGA transfer quota for this IP is spent - retry on a later run")
        _remove_tree(stage)
        return None

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
        records = file_records(self.text, dest, manifest, include_skipped=include_skipped)
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


def _reset_stage(stage: Path) -> None:
    _remove_tree(stage)
    stage.mkdir(parents=True, exist_ok=True)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), reverse=True):
        try:
            item.unlink() if item.is_file() else item.rmdir()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def _move_stage(stage: Path, dest: Path, moved: list[str]) -> None:
    if not stage.is_dir():
        return
    for item in stage.rglob("*"):
        if not item.is_file() or item.name.startswith(".megatmp."):
            continue
        rel = item.relative_to(stage)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        final = unique_path(target)
        try:
            item.replace(final)
            moved.append(str(final))
        except OSError:
            pass
