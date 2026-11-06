"""The pass loop: take the queue, run the lanes, fold the rows, come back.

Threads run the worker in this process. The legacy script spawned a child
PowerShell per URL; a thread is enough here because every engine call is a
subprocess and the orchestrator only polls, so a slow engine cannot block it.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paint, records, state
from .archive import append_pending, merge_pending
from .config import Paths, Runtime, Settings
from .destinations import apply_fallback, destination_for, free_gb, load_domains
from .engines import Engines, lane_status, lane_status_tail
from .lanes import LaneBoard
from .plan import build_plan, order_by_destination, read_queue
from .urls import Knowledge, host
from .worker import Worker

SPACE_STOP_GB = 10


def _local_now() -> datetime:
    """Local wall-clock time, timezone-aware (records store local time)."""
    return datetime.now().astimezone()


@dataclass
class Lane:
    slot: int
    url: str
    thread: threading.Thread
    result: Path
    archive: Path
    index: int = 0          # 1-based position in the queue, for the terminal


@dataclass
class PassStats:
    total: int = 0
    ok: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    offline: int = 0
    log: Path | None = None
    started: datetime = field(default_factory=_local_now)

    @property
    def ok_total(self) -> int:
        return sum(self.ok.values())

    def engine_summary(self) -> str:
        return ", ".join(f"{name}={count}" for name, count in sorted(self.ok.items()))


def say(message: str, paths=None) -> None:
    print(message, flush=True)
    if paths is None:
        return
    try:
        with (paths.logs / "dl8.log").open("a", encoding="utf-8", newline="") as fh:
            # the log mirrors the console without the colour codes
            fh.write(paint.strip(message) + "\r\n")
    except OSError:
        pass


def _fallback(
    runtime: Runtime, settings: Settings, out, stamp
) -> Settings:
    """Re-check the drive fallback and return the settings to use.

    ``apply_fallback`` only switches when the default destination's drive is
    below ``dest_fallback_below_gb`` and a fallback drive has room. Calling it
    at the start of every pass (and when the 10 GB guard trips) lets a
    long-running instance move new downloads without a restart.
    """
    refreshed = apply_fallback(settings)
    if refreshed is settings:
        return settings
    runtime.settings = refreshed
    path = refreshed.destinations[refreshed.default_dest].path
    out(paint.yellow(
        f"[{stamp()}] {refreshed.fallback_note} ({path})."
    ))
    return refreshed


def run_pass(
    runtime: Runtime,
    knowledge: Knowledge,
    engines: Engines,
    *,
    out=say,
) -> PassStats | None:
    """One pass. ``None`` when the queue held nothing usable."""
    paths = runtime.paths
    settings = runtime.settings
    filehosts = load_domains(paths.filehosts)
    stamp = lambda: time.strftime("%H:%M:%S")
    settings = _fallback(runtime, settings, out, stamp)

    urls = read_queue(paths.queue)

    if not settings.dry_run and not settings.no_capture:
        captured = state.read_clip_inbox(paths.clip_inbox)
        if captured:
            records.append_lines(paths.queue, captured)
            out(paint.cyan(f"[{stamp()}] clipboard: +{len(captured)} URL(s) queued."))
            urls += captured

    if not settings.dry_run and paths.staged_queue.exists():
        staged = read_queue(paths.staged_queue)
        fresh = [url for url in staged if url not in urls]
        if fresh:
            # An expanded listing belongs in queue.txt like any other link, so
            # the queue shows it while it runs and a restart resumes it.
            records.append_lines(paths.queue, fresh)
            urls += fresh
            out(paint.darkgray(
                f"[{stamp()}] {len(fresh)} staged URL(s) from queue-requeue.txt added to this run."
            ))
        try:
            paths.staged_queue.unlink()
        except OSError:
            pass

    if not urls:
        return None

    if state.archive_signature(paths.archive) != runtime.arc_sig:
        state.update_archive_sets(runtime, knowledge)
    merged = merge_pending(
        paths.fold_pending, paths.archive, runtime.seen, runtime.archived, knowledge,
        write=not settings.dry_run,
    )
    if not settings.dry_run:
        runtime.arc_sig = state.archive_signature(paths.archive)
    if merged.total:
        out(paint.darkgray(
            f"[{stamp()}] archive: {merged.total} row(s) left pending by the last run were merged."
        ))

    plan = build_plan(
        urls,
        seen=runtime.seen,
        failed=runtime.failed,
        skip_always=runtime.skip_always,
        knowledge=knowledge,
        archive_check=runtime.archive_check,
        retry_failed=settings.retry_failed,
    )
    if plan.unusable:
        out(paint.yellow(
            f"[{stamp()}] {plan.unusable} unusable queue line(s) dropped (not an http(s) URL)."
        ))
    if plan.skipped:
        out(paint.darkgray(f"[{stamp()}] {plan.summary()}"))
        if not settings.dry_run:
            records.write_lines(paths.queue, plan.urls)

    ordered, primary, secondary = order_by_destination(
        plan.urls, settings=settings, knowledge=knowledge
    )
    if primary and secondary:
        out(paint.darkgray(
            f"[{stamp()}] {primary} link(s) to the default destination first, "
            f"then {secondary} to the others."
        ))
    if not ordered:
        return None

    out(paint.cyan("dredge v8 - multi-engine downloader"))
    out(f"[{stamp()}] {len(ordered)} links queued.")

    stats = PassStats(total=len(ordered))
    stats.log = paths.logs / time.strftime("run-%Y%m%d-%H%M%S.log")
    _log_line(stats.log, f"[{stats.started:%Y-%m-%d %H:%M:%S}] dredge run - {len(ordered)} links queued")
    out(paint.darkgray(f"[{stamp()}] Run log: {stats.log}"))

    remaining = list(ordered)
    pending = list(range(len(ordered)))
    active: dict[int, Lane] = {}
    done = 0
    pending_rows = 0
    pending_since = time.monotonic()
    space_checked: dict[str, float] = {}
    full_dests: set[str] = set()
    progress_at = time.monotonic()
    progress_last: dict[int, tuple[str, bool]] = {}
    queued_canon = {key for u in ordered for key in knowledge.keys(u)}

    board = _make_board(out)
    if board:
        board.start()
    try:
        while pending or active:
            while len(active) < runtime.max_downloads and pending:
                index = _take_next(pending, ordered, active, knowledge)
                if index < 0:
                    break
                url = ordered[index]
                target = destination_for(url, knowledge, settings)
                if not settings.dry_run:
                    if target.name in full_dests:
                        continue        # leave it in queue.txt; this pass skips it
                    last = space_checked.get(target.name, 0.0)
                    if (time.monotonic() - last) >= 120:
                        space_checked[target.name] = time.monotonic()
                        free = free_gb(target.path)
                        if 0 <= free < SPACE_STOP_GB:
                            full_dests.add(target.name)
                            message = (
                                f"[{stamp()}] Stopping: only {free:.0f} GB free on "
                                f"{target.path.anchor} ({target.name}) - its links stay queued."
                            )
                            out(paint.yellow(message))
                            _log_line(stats.log, message)
                            refreshed = _fallback(runtime, settings, out, stamp)
                            if refreshed is not settings:
                                settings = refreshed
                                full_dests.clear()
                                space_checked.clear()
                            continue
                slot = _free_slot(active, runtime.max_downloads)
                if slot is None:
                    break
                lane_log = paths.lane_log(slot)
                try:
                    lane_log.write_text("", encoding="utf-8")
                except OSError:
                    pass
                lane_result = paths.lane_result(slot)
                lane_archive = paths.lane_archive(slot)
                for stale in (lane_result, lane_archive):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
                worker = Worker(
                    paths, settings, engines, knowledge,
                    filehosts=filehosts,
                    strict_generic=runtime.strict_generic,
                )
                thread = threading.Thread(
                    target=_run_worker,
                    args=(worker, url, target.path, target.layout == "flat", target.tpl,
                          not runtime.archive_check, lane_log, lane_result, lane_archive, slot),
                    daemon=True,
                )
                active[slot] = Lane(slot, url, thread, lane_result, lane_archive, index + 1)
                thread.start()
                if board:
                    board.set_url(slot, index + 1, len(ordered), url)

            _sleep(0.5, runtime, knowledge, board)
            picked = _catch_up_queue(
                paths, knowledge, ordered, pending, remaining, queued_canon, settings
            )
            if picked:
                out(paint.cyan(f"[{stamp()}] queue: +{len(picked)} URL(s) picked up."))
            now = time.monotonic()

            if board:
                board.ensure(range(1, runtime.max_downloads + 1), active)
                for slot in sorted(active):
                    name, pct, speed = lane_status_tail(paths.lane_log(slot))
                    lane = active[slot]
                    folder = _lane_folder_uri(paths, slot)
                    if name:
                        board.set_progress(
                            slot, lane.index, len(ordered), name, pct, speed,
                            folder=folder,
                        )
                    else:
                        board.set_url(
                            slot, lane.index, len(ordered), lane.url, folder=folder,
                        )
                for slot in range(1, runtime.max_downloads + 1):
                    if slot not in active:
                        board.idle(slot)
                board.refresh()

            if (
                board is None
                and settings.progress_every_seconds > 0
                and (now - progress_at) >= settings.progress_every_seconds
            ):
                progress_at = now
                for slot in sorted(active):
                    name, pct, speed = lane_status(paths.lane_log(slot))
                    key = (name, bool(speed))
                    if not name or progress_last.get(slot) == key:
                        continue
                    progress_last[slot] = key
                    extra = "  ".join(part for part in (pct, speed) if part)
                    detail = f"{name}  {extra}" if extra else name
                    lane = active[slot]
                    out(paint.darkgray(
                        f"[{stamp()}] {lane.index}/{len(ordered)}: lane {slot}: {detail}"
                    ))

            if not settings.no_capture and not settings.dry_run:
                captured = state.read_clip_inbox(paths.clip_inbox)
                if captured:
                    added, rejected = _capture(
                        captured, queued_canon, runtime, knowledge, settings
                    )
                    if added:
                        records.append_lines(paths.queue, added)
                        for url in added:
                            if url not in remaining:
                                remaining.append(url)
                                ordered.append(url)
                                pending.append(len(ordered) - 1)
                        out(paint.cyan(
                            f"[{stamp()}] clipboard: +{len(added)} URL(s) queued."
                        ))
                    if rejected:
                        total = sum(rejected.values())
                        parts = ", ".join(
                            f"{count} {reason}" for reason, count in rejected.items()
                        )
                        out(paint.darkgray(
                            f"[{stamp()}] clipboard: {total} URL(s) skipped ({parts})."
                        ))

            finished = [slot for slot, lane in active.items() if not lane.thread.is_alive()]
            for slot in finished:
                lane = active.pop(slot)
                progress_last.pop(slot, None)
                verdict = _verdict(lane.result)
                rows = _archive_rows(lane.archive)
                if rows:
                    _warn_pages(rows, out, stamp)
                    if not settings.dry_run:
                        pending_rows += append_pending(paths.fold_pending, rows)
                        if (
                            pending_rows >= settings.fold_batch_rows
                            or (time.monotonic() - pending_since) >= settings.fold_batch_seconds
                        ):
                            merged_now = merge_pending(
                                paths.fold_pending, paths.archive,
                                runtime.seen, runtime.archived, knowledge,
                            )
                            runtime.arc_sig = state.archive_signature(paths.archive)
                            pending_rows = 0
                            pending_since = time.monotonic()
                            if merged_now.total:
                                out(paint.darkgray(f"[{stamp()}] archive: {merged_now.total} row(s) merged."))

                if lane.url in remaining:
                    remaining.remove(lane.url)
                if not settings.dry_run:
                    records.write_lines(paths.queue, remaining)

                if verdict == "OFFLINE":
                    records.append_lines(paths.offline, [lane.url])
                    stats.offline += 1
                    _mark_failed(runtime, knowledge, lane.url)
                elif not verdict.startswith("OK|"):
                    records.append_lines(paths.fail, [lane.url])
                    stats.failed += 1
                    _mark_failed(runtime, knowledge, lane.url)
                else:
                    engine = verdict[3:]
                    stats.ok[engine] = stats.ok.get(engine, 0) + 1

                files = _files_summary(rows)
                if not files:
                    attempted = lane_status_tail(paths.lane_log(slot))[0]
                    if attempted and not attempted.lower().startswith(("http://", "https://")):
                        files = f"  -> {attempted}"
                done += 1
                # The file manifest and the lane's destination folder make the
                # finished line clickable: the name opens the file, "lane N"
                # opens the folder it landed in.
                made = _lane_files(paths.lane_files(slot))
                target_uri = paint.file_uri(made[0]) if len(made) == 1 else ""
                folder_uri = (
                    paint.file_uri(made[0].parent) if made
                    else _lane_folder_uri(paths, slot)
                )
                detail = f"{verdict}  {lane.url}{files}"
                _log_line(stats.log, f"[{stamp()}] {detail}")
                label = (
                    paint.link(f"lane {slot}", folder_uri) if folder_uri
                    else f"lane {slot}"
                )
                shown = (
                    paint.link(files, target_uri or folder_uri)
                    if files and (target_uri or folder_uri) else files
                )
                out(f"[{stamp()}] {done}/{len(ordered)}: {label}: {verdict}  "
                    f"{lane.url}{shown}")
                if board:
                    board.set_done(
                        slot, lane.index, len(ordered), verdict, files,
                        folder=folder_uri, target=target_uri,
                    )

        if not settings.dry_run:
            merged_end = merge_pending(
                paths.fold_pending, paths.archive, runtime.seen, runtime.archived, knowledge
            )
            runtime.arc_sig = state.archive_signature(paths.archive)
            if merged_end.total:
                out(paint.darkgray(f"[{stamp()}] archive: {merged_end.total} row(s) merged."))

    finally:
        if board:
            board.stop()
    end = _local_now()
    elapsed = end - stats.started
    _log_line(
        stats.log,
        f"[{end:%Y-%m-%d %H:%M:%S}] Finished in {elapsed}. OK={stats.ok_total} "
        f"[{stats.engine_summary()}] FAILED={stats.failed} OFFLINE={stats.offline}",
    )
    out(paint.green(f"[{stamp()}] Finished."))
    out(paint.darkgray(f"Run log: {stats.log}"))
    return stats


def _make_board(out) -> LaneBoard | None:
    """A live block only when the default printer is on an interactive console."""
    if out is say and sys.stdout.isatty():
        return LaneBoard()
    return None


def _run_worker(
    worker: Worker,
    url: str,
    dest: Path,
    flat: bool,
    dest_tpl: str,
    ignore: bool,
    lane_log: Path,
    result: Path,
    archive: Path,
    slot: int,
) -> None:
    try:
        worker.download(
            url, dest=dest, flat=flat, dest_tpl=dest_tpl, ignore_records=ignore,
            lane_log=lane_log, result=result, archive=archive, slot=slot,
        )
    except Exception as exc:  # noqa: BLE001 - one URL must never kill the pass
        try:
            result.write_text("FAILED", encoding="utf-8")
        except OSError:
            pass
        try:
            with lane_log.open("a", encoding="utf-8", newline="") as fh:
                fh.write(f"[dredge] worker error: {exc}\r\n")
        except OSError:
            pass


def _verdict(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "FAILED"
    return text or "FAILED"


def _archive_rows(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _files_summary(rows: list[str]) -> str:
    names: list[str] = []
    for row in rows:
        fields = row.split("\t")
        if len(fields) >= 3 and fields[2]:
            names.extend(n for n in fields[2].split(";") if n)
    names = list(dict.fromkeys(names))
    if not names:
        return ""
    return "  -> " + "; ".join(names)


def _lane_files(path: Path) -> list[Path]:
    """Absolute paths the lane wrote, from its ``laneN.files`` manifest."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [Path(line.strip()) for line in lines if line.strip()]


def _read_path(path: Path) -> Path | None:
    """A path a lane published (its destination folder or its current target)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return Path(text) if text else None


def _lane_folder_uri(paths: Paths, slot: int) -> str:
    """Folder link for a running lane: the file's own folder, else the root.

    laneN.target holds the path the engine is about to write, so it already
    carries the creator subfolder the output template adds. laneN.dest is only
    the destination root, and is used until that path is known.
    """
    target = _read_path(paths.lane_target(slot))
    if target is not None:
        folder = paint.file_uri(target.parent)
        if folder:
            return folder
    here = _read_path(paths.lane_dest(slot))
    return paint.file_uri(here) if here else ""


def _warn_pages(rows: list[str], out, stamp) -> None:
    for row in rows:
        fields = row.split("\t")
        if len(fields) >= 3 and fields[2]:
            count = len([n for n in fields[2].split(";") if n])
            if count > 8:
                out(paint.yellow(
                    f"[{stamp()}] WARNING: one URL produced {count} files (looks like a page "
                    f"listing, not a single video): {fields[1]}"
                ))


def _mark_failed(runtime: Runtime, knowledge: Knowledge, url: str) -> None:
    keys = knowledge.keys(url)
    if not keys:
        return
    runtime.failed.update(keys)
    ident = knowledge.seen_id(keys[0])
    if ident:
        runtime.failed.add(ident)


def _catch_up_queue(
    paths: Paths,
    knowledge: Knowledge,
    ordered: list[str],
    pending: list[int],
    remaining: list[str],
    queued_canon: set[str],
    settings,
) -> list[str]:
    """Pick up URLs written to queue.txt while this pass is running.

    The pass takes its plan once, but the menu's retry-failed action (and an
    edit of queue.txt) can add URLs while lanes are busy. Without this, the
    next completion rewrites queue.txt from ``remaining`` and the new lines
    would be dropped.

    An expanded listing is staged in ``queue-requeue.txt`` by the worker
    thread, which cannot append to queue.txt while the pass rewrites it. Those
    lines are moved into queue.txt here, before they are scheduled, so the
    queue always shows every link this pass is working on and a restart
    mid-pass resumes them.
    """
    if settings.dry_run:
        return []
    if paths.staged_queue.exists():
        fresh: list[str] = []
        seen: set[str] = set()
        for url in read_queue(paths.staged_queue):
            keys = knowledge.keys(url)
            if not keys or any(key in queued_canon or key in seen for key in keys):
                continue
            seen.update(keys)
            fresh.append(url)
        if fresh:
            records.append_lines(paths.queue, fresh)
        try:
            paths.staged_queue.unlink()
        except OSError:
            pass
    added: list[str] = []
    for url in records.read_lines(paths.queue):
        keys = knowledge.keys(url)
        if not keys or any(key in queued_canon for key in keys):
            continue
        queued_canon.update(keys)
        ordered.append(url)
        pending.append(len(ordered) - 1)
        remaining.append(url)
        added.append(url)
    return added


def _capture(
    urls: list[str],
    queued: set[str],
    runtime: Runtime,
    knowledge: Knowledge,
    settings,
) -> tuple[list[str], dict[str, int]]:
    """Queue new clipboard URLs; also count why the others were left out."""
    added: list[str] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for url in urls:
        if not url.lower().startswith(("http://", "https://")):
            reject("not a URL")
            continue
        keys = knowledge.keys(url)
        if not keys:
            reject("not a URL")
            continue
        if url in queued or any(key in queued for key in keys):
            reject("already queued")
            continue
        ident = knowledge.seen_id(keys[0])
        if runtime.archive_check and (
            url in runtime.seen
            or any(key in runtime.seen for key in keys)
            or (ident and ident in runtime.seen)
        ):
            reject("already downloaded")
            continue
        if runtime.archive_check and (
            url in runtime.skip_always
            or any(key in runtime.skip_always for key in keys)
            or (ident and ident in runtime.skip_always)
        ):
            reject("in skip.txt")
            continue
        if runtime.archive_check and not settings.retry_failed and (
            url in runtime.failed
            or any(key in runtime.failed for key in keys)
            or (ident and ident in runtime.failed)
        ):
            reject("failed before")
            continue
        queued.update(keys)
        added.append(url)
    return added, rejected


def _serial(url: str, knowledge: Knowledge) -> bool:
    rule = knowledge.rule(url)
    return bool(rule and rule.serial)


def _take_next(
    pending: list[int],
    urls: list[str],
    active: dict[int, Lane],
    knowledge: Knowledge,
) -> int:
    busy = {
        host(lane.url)
        for lane in active.values()
        if _serial(lane.url, knowledge)
    }
    for pos, index in enumerate(pending):
        if host(urls[index]) in busy:
            continue
        pending.pop(pos)
        return index
    return -1


def _free_slot(active: dict[int, Lane], maximum: int) -> int | None:
    if maximum < 1:
        return None
    for slot in range(1, maximum + 1):
        if slot not in active:
            return slot
    return None


def _log_line(path: Path | None, line: str) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\r\n")
    except OSError:
        pass


def _sleep(
    seconds: float, runtime: Runtime, knowledge: Knowledge, board: LaneBoard | None = None
) -> None:
    from . import menu
    from .tui import show_dredge_menu

    def open_menu() -> None:
        if board:
            board.stop()
        try:
            show_dredge_menu(runtime, knowledge)
        finally:
            if board:
                board.start()

    menu.wait(seconds, open_menu)
