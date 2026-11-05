"""Command line entry point and the pass loop.

Flags mirror the legacy PowerShell script, so a shortcut that ran
``dredge.ps1 -Once -DryRun`` runs ``dredge --once --dry-run`` here.

With an empty queue dredge waits instead of exiting: ``queue.txt`` and the
clipboard inbox are polled and the next pass starts by itself. ``--once``
restores the take-the-queue-once-and-leave behaviour.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import __version__, clipboard, cookies, menu, paint, records, state
from .archive import append_pending, merge_pending
from .config import Paths, Runtime, Settings, write_gallery_runtime
from .destinations import apply_fallback, load_domains
from .engines import Engines, run
from .lock import AlreadyRunning, InstanceLock
from .orchestrator import run_pass
from .probe import run_mode
from .tui import show_dredge_menu
from .urls import Knowledge
from .worker import Worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dredge",
        description=(
            "Queue and archive downloads from yt-dlp, gallery-dl and file hosts. "
            "No site is named in the code: per-site knowledge lives in config/sites.txt."
        ),
    )
    parser.add_argument("--root", default=None, help="folder holding config/, data/, logs/")
    parser.add_argument("--once", action="store_true",
                        help="take the queue once and exit (default: stay and wait for links)")
    parser.add_argument("--dry-run", action="store_true",
                        help="probe only (--simulate); never writes records")
    parser.add_argument("--menu", action="store_true", help="open the console menu at startup")
    parser.add_argument("--retry-failed", action="store_true",
                        help="try URLs already in fail.txt/offline.txt again")
    parser.add_argument("--probe", action="store_true",
                        help="probe mode: list copied creator pages and queue their items")
    parser.add_argument("--probe-files", action="store_true",
                        help="probe mode, file level: queue each picture/attachment URL")
    parser.add_argument("--max-downloads", type=int, default=None, metavar="N",
                        help="concurrent downloads (default: config or 4)")
    parser.add_argument("--idle-seconds", type=int, default=None, metavar="N",
                        help="how often to look for new links while idle (default 3)")
    parser.add_argument("--progress-every", type=int, default=None, metavar="N",
                        help="seconds between lane status lines when output is not a terminal "
                             "(0 = off, default 15)")
    parser.add_argument("--no-capture", action="store_true", help="do not watch the clipboard")
    parser.add_argument("--no-update", action="store_true", help="skip the engine self-update")
    parser.add_argument("--no-cookies", action="store_true", help="skip the cookie refresh")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip the 'Press Enter to close' prompt (for scripting)")
    parser.add_argument("--strict-generic", action="store_true",
                        help="strict mode: a generic-extractor result only counts on hosts "
                             "in generic-sites.txt")
    parser.add_argument("--ignore-records", action="store_true",
                        help="ignore downloads.tsv, fail/skip lists and the engine archives")
    parser.add_argument("--refresh", action="append", default=[], metavar="URL",
                        help="re-check one post against gallery-dl's archive "
                             "(new/changed files are fetched as .1)")
    parser.add_argument("--version", action="version", version=__version__)

    worker = parser.add_argument_group("worker (internal - one URL)")
    worker.add_argument("--worker", action="store_true", help="download one URL and exit")
    worker.add_argument("--url", default="", help="URL for --worker")
    worker.add_argument("--dest", default="", help="destination root for --worker")
    worker.add_argument("--flat", action="store_true",
                        help="--dest uses the flat layout (no per-creator folder)")
    worker.add_argument("--lane-log", default="", help="lane log file")
    worker.add_argument("--result", default="", help="file that receives the verdict")
    worker.add_argument("--archive", default="", help="file that receives the archive rows")
    return parser


def _safe_console() -> None:
    """A filename the console codepage cannot encode must not kill the pass."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)
    paths = Paths.from_root(args.root)
    settings = Settings.load(
        paths,
        dry_run=True if args.dry_run else None,
        once=True if args.once else None,
        max_downloads=args.max_downloads,
        idle_seconds=args.idle_seconds,
        progress_every_seconds=args.progress_every,
        no_capture=True if args.no_capture else None,
        retry_failed=True if args.retry_failed else None,
        strict_generic=True if args.strict_generic else None,
        ignore_records=True if args.ignore_records else None,
        no_prompt=True if args.no_prompt else None,
    )
    knowledge = Knowledge.load(paths.config)
    engines = Engines.discover(settings)

    if args.worker:
        return run_worker(paths, settings, knowledge, engines, args)

    lock = InstanceLock(paths.lock)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        pid = f" (pid {exc.pid})" if exc.pid else ""
        print(paint.red(
            f"another dredge is already running{pid} - close it before starting a second one."
        ))
        return 3
    try:
        return _run_instance(paths, settings, knowledge, engines, args)
    finally:
        lock.release()


def _run_instance(
    paths: Paths,
    settings: Settings,
    knowledge: Knowledge,
    engines: Engines,
    args: argparse.Namespace,
) -> int:
    settings = apply_fallback(settings)
    if settings.fallback_note:
        print(paint.yellow(
            f"[{_clock()}] {settings.fallback_note} "
            f"({settings.destinations[settings.default_dest].path})."
        ))

    try:
        prepare(paths, settings, knowledge, engines, args)
    except SystemExit as exc:
        return int(exc.code or 0)

    runtime = Runtime(paths, settings)

    if args.refresh:
        return run_refresh(runtime, knowledge, engines, args.refresh)

    if args.probe or args.probe_files:
        return run_probe(runtime, knowledge, engines, files=args.probe_files)

    if args.menu:
        show_dredge_menu(
            runtime, knowledge,
            probe_files=lambda: run_probe(runtime, knowledge, engines, files=True),
            refresh_post=refresh_callback(runtime, knowledge, engines),
        )
        if not (settings.once or settings.dry_run):
            return 0

    return run_loop(runtime, knowledge, engines)


def prepare(
    paths: Paths,
    settings: Settings,
    knowledge: Knowledge,
    engines: Engines,
    args: argparse.Namespace,
) -> None:
    """Startup: folders, generated config, dependency check, engines, cookies."""
    paths.ensure()
    write_gallery_runtime(paths, settings)
    for path in (paths.queue, paths.fail, paths.offline, paths.skip):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    sort_records(paths)

    fatal: list[str] = []
    warn: list[str] = []
    if not engines.yt:
        fatal.append("yt-dlp not found. Install it with: winget install yt-dlp.yt-dlp")
    if not engines.gdl:
        warn.append("gallery-dl not found (tier 1 skipped). Install: winget install mikf.gallery-dl")
    if not engines.megadl:
        warn.append("megatools not found (MEGA links, engine=megadl, will fail)")
    if not engines.mdrs:
        warn.append("mediafire_rs not found (MediaFire links, engine=mdrs, will fail)")
    for conf in (paths.ytdlp_conf, paths.ytdlp_generic_conf):
        if not conf.exists():
            fatal.append(f"config not found: {conf}")
    if not paths.gallery_conf.exists():
        warn.append(f"gallery-dl.conf not found: {paths.gallery_conf}")
    if not paths.cookies.exists():
        warn.append(f"cookies.txt not found: {paths.cookies} (some sites may fail)")
    for dest in settings.destinations.values():
        anchor = Path(dest.path).anchor
        if anchor and not Path(anchor).exists():
            fatal.append(f"destination drive not found: {dest.path} ({dest.name})")

    for dest in settings.destinations.values():
        try:
            Path(dest.path).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    for dest in settings.destinations.values():
        clean_staging(dest.path)

    if fatal:
        print(paint.yellow("\nCannot start - the following are missing. Edit config/dredge.conf."))
        for line in fatal:
            print(paint.red(f"  - {line}"))
        raise SystemExit(1)
    for line in warn:
        print(paint.yellow(f"WARNING: {line}"))

    if not args.no_update and not settings.dry_run:
        print(paint.darkgray("Updating engines..."))
        if engines.yt:
            run([engines.yt, "--ignore-config", "-U"], cwd=paths.root)
        if engines.gdl:
            run([engines.gdl, "--config-ignore", "-U"], cwd=paths.root)

    if not args.no_cookies and not settings.dry_run:
        message = cookies.refresh(paths, knowledge, engines.gdl, settings.cookies_browser)
        print(paint.darkgray(message))


def sort_records(paths: Paths) -> None:
    """Put queue/fail/offline/skip in alphabetical order at startup.

    Only the URL lines are sorted, case-insensitively; comments stay at the
    top and blank lines are dropped.
    """
    for path in (paths.queue, paths.fail, paths.offline, paths.skip):
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        urls = [
            line.strip() for line in raw
            if line.strip() and not line.strip().startswith("#")
        ]
        if not urls:
            continue
        ordered = sorted(urls, key=str.casefold)
        comments = [line for line in raw if line.strip().startswith("#")]
        if ordered == urls and not comments:
            continue
        text = "".join(line + "\r\n" for line in comments + ordered)
        try:
            path.write_text(text, encoding="utf-8", newline="")
        except OSError:
            pass


def clean_staging(dest: Path) -> None:
    """Remove ``~dl-laneN`` / ``~fh-laneN`` folders left by a killed worker.

    File-host downloads now write into the destination itself, so these are
    only the in-destination staging folders from older versions.
    """
    folder = Path(dest)
    if not folder.is_dir():
        return
    for stale in folder.glob("~*lane*"):
        if not stale.is_dir() or not (stale.name.startswith("~dl-lane")
                                      or stale.name.startswith("~fh-lane")):
            continue
        try:
            size = sum(f.stat().st_size for f in stale.rglob("*") if f.is_file())
            print(paint.darkgray(
                f"    clearing leftover staging folder {stale.name} ({size / 1024**2:.1f} MB)"
            ))
        except OSError:
            pass
        shutil.rmtree(stale, ignore_errors=True)


def run_probe(
    runtime: Runtime, knowledge: Knowledge, engines: Engines, *, files: bool
) -> int:
    """``--probe`` / menu 1 -> 4: list pages, queue new items, keep downloads paused."""
    return run_mode(runtime, knowledge, engines, files=files)


def run_worker(
    paths: Paths,
    settings: Settings,
    knowledge: Knowledge,
    engines: Engines,
    args: argparse.Namespace,
) -> int:
    """``--worker``: one URL, the whole tier chain, one verdict file."""
    if not args.url:
        print("--worker needs --url")
        return 2
    paths.ensure()
    worker = Worker(
        paths,
        settings,
        engines,
        knowledge,
        filehosts=load_domains(paths.filehosts),
    )
    verdict = worker.download(
        args.url,
        dest=Path(args.dest) if args.dest else None,
        flat=args.flat,
        ignore_records=args.ignore_records,
        refresh=bool(args.refresh),
        lane_log=args.lane_log or None,
        result=args.result or None,
        archive=args.archive or None,
    )
    print(verdict)
    return 0


def refresh_callback(runtime: Runtime, knowledge: Knowledge, engines: Engines):
    """The menu's "refresh post" action: one URL, gallery-dl archive-aware."""

    def refresh_post(url: str) -> None:
        # The menu runs while a pass may be active; appending the rows is safe,
        # but rewriting downloads.tsv here would race with the orchestrator's
        # next merge. Leave the merge to the pass.
        run_refresh(runtime, knowledge, engines, [url], merge=False)

    return refresh_post


def run_refresh(
    runtime: Runtime,
    knowledge: Knowledge,
    engines: Engines,
    urls: list[str],
    *,
    merge: bool = True,
) -> int:
    """``--refresh`` / menu 1 -> 9: re-check a post against gallery-dl's archive.

    The TSV is not consulted, but gallery-dl's archive is. With the hash-bearing
    ``archive-format`` for kemono/pawchive, an unchanged file keeps its archive
    key and is skipped; a changed or new attachment has a new key and downloads.
    ``skip=enumerate`` writes it as ``name.1.ext`` instead of overwriting, and the
    worker records that name in the TSV.
    """
    paths = runtime.paths
    settings = runtime.settings
    if not settings.dry_run:
        state.update_archive_sets(runtime, knowledge)
    worker = Worker(
        paths, settings, engines, knowledge,
        filehosts=load_domains(paths.filehosts),
    )
    lane_log = paths.logs / "refresh.log"
    result = paths.logs / "refresh.result"
    lane_archive = paths.logs / "refresh.archive"
    ok = 0
    for url in urls:
        for stale in (result, lane_archive):
            try:
                stale.unlink()
            except OSError:
                pass
        print(paint.darkgray(f"[{_clock()}] refresh: {url}"))
        verdict = worker.download(
            url, refresh=True, lane_log=lane_log, result=result,
            archive=lane_archive,
        )
        try:
            rows = [
                line.strip() for line in lane_archive.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError:
            rows = []
        if rows and not settings.dry_run:
            append_pending(paths.fold_pending, rows)
            if merge:
                merge_pending(
                    paths.fold_pending, paths.archive,
                    runtime.seen, runtime.archived, knowledge,
                )
        if verdict.startswith("OK|"):
            ok += 1
            _clear_failure(paths, knowledge, url)
        print(f"[{_clock()}] {verdict}  {url}")
    if ok:
        state.update_failed_set(runtime, knowledge)
    return 0 if ok == len(urls) else 1


def _clear_failure(paths: Paths, knowledge: Knowledge, url: str) -> None:
    """A successful refresh resolves a previous failure record."""
    key = knowledge.canon(url)
    for path in (paths.fail, paths.offline):
        lines = records.read_lines(path)
        kept = [line for line in lines if knowledge.canon(line) != key]
        if len(kept) != len(lines):
            records.write_lines(path, kept)


def run_loop(runtime: Runtime, knowledge: Knowledge, engines: Engines) -> int:
    """The pass loop: one pass, then wait for the next queue line."""
    paths = runtime.paths
    settings = runtime.settings
    opened: list[bool] = []

    def open_menu() -> None:
        if opened:
            return
        opened.append(True)
        try:
            show_dredge_menu(
                runtime, knowledge,
                probe_files=lambda: run_probe(runtime, knowledge, engines, files=True),
                refresh_post=refresh_callback(runtime, knowledge, engines),
            )
        finally:
            opened.clear()

    def pause(seconds: float) -> None:
        menu.wait(seconds, open_menu)

    if not settings.dry_run:
        state.update_archive_sets(runtime, knowledge)
    state.update_failed_set(runtime, knowledge)

    capture = _start_capture(runtime)

    while True:
        if paths.paused.exists():
            if not runtime.pause_shown:
                print(paint.yellow(
                    f"[{_clock()}] downloads are paused - queue.txt is waiting for review "
                    "(menu 1 -> 6 resumes)."
                ))
                runtime.pause_shown = True
            if settings.once:
                break
            pause(1.0)
            continue
        runtime.pause_shown = False

        stats = run_pass(runtime, knowledge, engines)
        if stats is None:
            if settings.once or settings.dry_run:
                print(paint.darkgray("Queue is empty."))
                break
            print(paint.darkgray(
                f"[{_clock()}] queue is empty - waiting for new links (clipboard or queue.txt)."
            ))
            state.wait_for_change(
                paths.queue, paths.clip_inbox, max(1, settings.idle_seconds), sleep=pause
            )
            continue
        if settings.once or settings.dry_run:
            break

    if capture:
        capture.stop()

    if not settings.no_prompt and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close")
        except EOFError:
            pass
    return 0


def _start_capture(runtime: Runtime) -> clipboard.Capture | None:
    """Start the clipboard watcher that feeds clip-inbox.txt, when enabled."""
    settings = runtime.settings
    if settings.dry_run or settings.no_capture:
        return None
    if not clipboard.available():
        print(paint.yellow(
            f"[{_clock()}] clipboard capture off: pyperclip is not installed "
            "(pip install pyperclip, or queue links by hand)."
        ))
        return None
    capture = clipboard.Capture(runtime.paths.clip_inbox)
    capture.start()
    print(paint.darkgray(f"[{_clock()}] clipboard capture on: copy a URL and it joins the queue."))
    return capture


def _clock() -> str:
    return time.strftime("%H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
