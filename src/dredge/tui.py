"""The menus: checks, files, and the top level.

Text and order match the legacy console menu, including the lane prompt and the
records-check wording, so the same muscle memory works in both implementations.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import menu, paint, records, state
from .config import Runtime
from .plan import split_line
from .report import write_reports
from .urls import Knowledge


def _say(message: str, color: str = "") -> None:
    print(f"    {paint.paint(message, color)}")


def toggle_paused(runtime: Runtime, knowledge: Knowledge) -> tuple[bool, str]:
    """Pause or resume downloads; returns ``(now_paused, message)``.

    The flag is ``data/state/paused``. Pausing does not kill a running lane -
    it stops new lanes from starting, so the queue waits and active files can
    finish. Resuming rebuilds the failed set, which may have changed while the
    queue was held.
    """
    pause = runtime.paths.paused
    if pause.exists():
        try:
            pause.unlink()
        except OSError:
            pass
        state.update_failed_set(runtime, knowledge)
        runtime.pause_shown = False
        return False, "downloads resumed - queue.txt is live again."
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.touch()
    runtime.pause_shown = True
    return True, "downloads paused - queue.txt waits; press P to resume."


def show_checks_menu(
    runtime: Runtime,
    knowledge: Knowledge,
    *,
    probe_files: Callable[[], None] | None = None,
) -> None:
    while True:
        paused = runtime.paths.paused.exists()
        fail_count = len(records.read_lines(runtime.paths.fail))
        skip_count = len(records.read_lines(runtime.paths.skip))
        items = [
            menu.MenuItem(
                "1",
                f"records check   [{'ON' if runtime.archive_check else 'OFF'}]  "
                "skip anything already downloaded, failed or skipped",
                color="gray" if runtime.archive_check else "yellow",
            ),
            menu.MenuItem(
                "2", f"retry failed    move fail.txt back into the queue ({fail_count} link(s))"
            ),
            menu.MenuItem(
                "3", f"retry skipped   move skip.txt back into the queue ({skip_count} link(s))"
            ),
            menu.MenuItem("4", "skip failed     move fail.txt into skip.txt (never retried)"),
            menu.MenuItem("5", "probe files     copy links; queue each picture/attachment URL"),
            menu.MenuItem(
                "6", f"lanes           [{runtime.max_downloads}]  concurrent downloads "
                     "(asks for a number)"
            ),
            menu.MenuItem(
                "7", f"downloads       [{'PAUSED' if paused else 'running'}]  "
                     "pause/resume (P)"
            ),
            menu.MenuItem(
                "8", f"strict mode     [{'ON' if runtime.strict_generic else 'OFF'}]  "
                     "the generic tier only runs on hosts in generic-sites.txt"
            ),
        ]
        choice = menu.select("checks", items, close="back")
        if choice is None:
            return
        if choice == "1":
            runtime.archive_check = not runtime.archive_check
            if runtime.archive_check:
                _say("records check ON - downloads.tsv, fail.txt, skip.txt and the engine "
                     "archives are honoured.", "green")
            else:
                _say("records check OFF - every previous record is ignored: queued links are "
                     "fetched again from scratch.", "yellow")
        elif choice == "2":
            retry_failed(runtime, knowledge)
        elif choice == "3":
            retry_skipped(runtime, knowledge)
        elif choice == "4":
            skip_failed(runtime, knowledge)
        elif choice == "5":
            if probe_files:
                probe_files()
            else:
                _say("probe mode needs the engines - run dredge with --probe-files.", "yellow")
        elif choice == "6":
            _say(f"lanes now: {runtime.max_downloads}")
            answer = menu.prompt_line("    concurrent downloads (1-16, Enter keeps it) ").strip()
            if not answer:
                _say("lanes unchanged.", "darkgray")
            else:
                try:
                    value = int(answer)
                except ValueError:
                    value = 0
                if 1 <= value <= 16:
                    runtime.max_downloads = value
                    state.save_lanes(runtime.paths, value)
                    _say(f"lanes set to {value} - saved; the next free slot uses it.", "green")
                else:
                    _say("lanes must be a number from 1 to 16.", "yellow")
        elif choice == "7":
            paused, message = toggle_paused(runtime, knowledge)
            _say(message, "yellow" if paused else "green")
        elif choice == "8":
            runtime.strict_generic = not runtime.strict_generic
            if runtime.strict_generic:
                _say("strict mode ON - a yt-dlp tier that falls back to the generic "
                     "extractor only counts on hosts in generic-sites.txt.")
            else:
                _say("strict mode OFF - the generic tier may handle any host.")


def latest_run_log(logs: Path) -> Path | None:
    """Newest ``run-*.log`` in the logs folder, or ``None`` when there is none."""
    latest = None
    newest = -1.0
    for path in logs.glob("run-*.log"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp > newest:
            latest, newest = path, stamp
    return latest


def show_files_menu(runtime: Runtime) -> None:
    paths = runtime.paths
    files = [
        menu.MenuItem("1", "downloads.tsv   what was downloaded (the archive)"),
        menu.MenuItem("2", "queue.txt       links waiting"),
        menu.MenuItem("3", "fail.txt        tried, did not work"),
        menu.MenuItem("4", "offline.txt     the post is gone"),
        menu.MenuItem("5", "skip.txt        never download these again"),
        menu.MenuItem("6", "run log         the current/latest run"),
    ]
    mapping = {
        "1": paths.archive, "2": paths.queue, "3": paths.fail,
        "4": paths.offline, "5": paths.skip,
    }
    while True:
        choice = menu.select("files", files, close="back")
        if choice is None:
            return
        if choice == "6":
            log = latest_run_log(paths.logs)
            if log is None:
                _say("no run log yet.", "darkgray")
            else:
                open_record(log)
            continue
        target = mapping.get(choice)
        if target:
            open_record(target)


def open_record(path: Path) -> None:
    if not path.exists():
        _say(f"missing: {path}", "yellow")
        return
    try:
        if sys.platform == "win32":
            # a .tsv has no default handler on a normal Windows box
            if path.suffix.lower() == ".tsv":
                os.startfile(str(path), "open")  # type: ignore[attr-defined]
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            print(f"    {path}")
    except OSError:
        _say("could not open it", "red")


def show_dredge_menu(
    runtime: Runtime,
    knowledge: Knowledge,
    *,
    probe_files: Callable[[], None] | None = None,
) -> None:
    items = [
        menu.MenuItem(
            "1", "checks    records check, retry failed/skipped, skip failed, probe files, lanes, pause"
        ),
        menu.MenuItem("2", "files     downloads.tsv, queue.txt, fail.txt, offline.txt, skip.txt, run log"),
        menu.MenuItem("3", "tools     url scan (JSON -> reports/), record report (fail/skip/offline/downloads HTML)"),
    ]
    while True:
        choice = menu.select("dredge menu", items)
        if choice is None:
            return
        if choice == "1":
            show_checks_menu(
                runtime, knowledge,
                probe_files=probe_files,
            )
        elif choice == "2":
            show_files_menu(runtime)
        elif choice == "3":
            show_tools_menu(runtime)


def show_tools_menu(runtime: Runtime) -> None:
    items = [
        menu.MenuItem(
            "1",
            "url scan       JSON metadata in the default destination -> "
            "reports/url-scan-*.tsv (the JSON is deleted; mega/mediafire/"
            "drive links go to queue.txt)",
        ),
        menu.MenuItem("2", "record report  fail/skip/offline/downloads HTML in reports/ (opens them)"),
    ]
    while True:
        choice = menu.select("tools", items, close="back")
        if choice is None:
            return
        if choice == "1":
            run_url_scan(runtime)
        elif choice == "2":
            run_record_report(runtime)


def run_url_scan(runtime: Runtime) -> None:
    """Run ``tools/url_scan.py`` against the default destination."""
    script = runtime.paths.tools / "url_scan.py"
    if not script.exists():
        _say(f"missing: {script}", "yellow")
        return
    _say("scanning the default destination for JSON metadata ...", "darkgray")
    try:
        code = subprocess.call(
            [sys.executable, str(script), "--root", str(runtime.paths.root)]
        )
    except OSError as exc:
        _say(f"could not run it: {exc}", "red")
        return
    if code != 0:
        _say(f"url scan exited with {code}.", "red")


def run_record_report(runtime: Runtime) -> None:
    try:
        written = write_reports(runtime.paths.root, open_pages=True)
    except OSError as exc:
        _say(f"could not write the reports: {exc}", "red")
        return
    for name, path in written.items():
        _say(f"{name}: {path}", "green")


def retry_skipped(runtime: Runtime, knowledge: Knowledge) -> None:
    """Move every skip.txt URL back into the queue and empty skip.txt."""
    paths = runtime.paths
    if not paths.skip.exists():
        _say("skip.txt is missing.", "yellow")
        return
    skipped = records.read_lines(paths.skip)
    if not skipped:
        _say("skip.txt has nothing to retry.", "darkgray")
        return
    queued = records.keyed_set(records.read_lines(paths.queue), knowledge)
    add = [
        line for line in skipped
        if not any(key in queued for key in knowledge.keys(split_line(line)[0]))
    ]
    try:
        # queue first, skip.txt second: if the queue cannot be written nothing is lost
        if add:
            records.append_lines(paths.queue, add)
        records.write_lines(paths.skip, [])
    except OSError as exc:
        _say(f"could not update the records: {exc}", "red")
        return
    state.update_failed_set(runtime, knowledge)
    _say(f"{len(add)} link(s) moved from skip.txt into the queue.", "green")
    if len(skipped) > len(add):
        _say(f"{len(skipped) - len(add)} were already queued.", "darkgray")


def retry_failed(runtime: Runtime, knowledge: Knowledge) -> None:
    """Move every fail.txt URL back into the queue and empty fail.txt."""
    paths = runtime.paths
    if not paths.fail.exists():
        _say("fail.txt is missing.", "yellow")
        return
    failed = records.read_lines(paths.fail)
    if not failed:
        _say("fail.txt has nothing to retry.", "darkgray")
        return
    queued = records.keyed_set(records.read_lines(paths.queue), knowledge)
    add = [
        line for line in failed
        if not any(key in queued for key in knowledge.keys(split_line(line)[0]))
    ]
    try:
        # queue first, fail.txt second: if the queue cannot be written nothing is lost
        if add:
            records.append_lines(paths.queue, add)
        records.write_lines(paths.fail, [])
    except OSError as exc:
        _say(f"could not update the records: {exc}", "red")
        return
    state.update_failed_set(runtime, knowledge)
    _say(f"{len(add)} link(s) moved from fail.txt into the queue.", "green")
    if len(failed) > len(add):
        _say(f"{len(failed) - len(add)} were already queued.", "darkgray")


def skip_failed(runtime: Runtime, knowledge: Knowledge) -> None:
    """Move every fail.txt URL into skip.txt: never retried again."""
    paths = runtime.paths
    if not paths.fail.exists():
        _say("fail.txt is missing.", "yellow")
        return
    failed = records.read_lines(paths.fail)
    if not failed:
        _say("fail.txt has nothing to skip.", "darkgray")
        return
    known = records.keyed_set(records.read_lines(paths.skip), knowledge)
    add = [
        line for line in failed
        if not any(key in known for key in knowledge.keys(split_line(line)[0]))
    ]
    try:
        # skip.txt first, fail.txt second: if the write fails nothing is lost
        if add:
            records.append_lines(paths.skip, add)
        records.write_lines(paths.fail, [])
    except OSError as exc:
        _say(f"could not update the records: {exc}", "red")
        return
    state.update_failed_set(runtime, knowledge)
    _say(f"{len(add)} link(s) moved from fail.txt to skip.txt (never retried).", "green")
    if len(failed) > len(add):
        _say(f"{len(failed) - len(add)} were already in skip.txt.", "darkgray")
