"""The pass loop: take the queue, run the lanes, fold the rows, come back.

Threads run the worker in this process. The legacy script spawned a child
PowerShell per URL; a thread is enough here because every engine call is a
subprocess and the orchestrator only polls, so a slow engine cannot block it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import records, state
from .archive import append_pending, merge_pending
from .config import Runtime
from .destinations import destination, free_gb, load_domains
from .engines import Engines, lane_status
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
            fh.write(message + "\r\n")
    except OSError:
        pass


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
    domains = load_domains(paths.pornsites)
    filehosts = load_domains(paths.filehosts)
    stamp = lambda: time.strftime("%H:%M:%S")

    urls = read_queue(paths.queue)

    if not settings.dry_run and not settings.no_capture:
        captured = state.read_clip_inbox(paths.clip_inbox)
        if captured:
            records.append_lines(paths.queue, captured)
            out(f"[{stamp()}] clipboard: +{len(captured)} URL(s) queued.")
            urls += captured

    if not settings.dry_run and paths.staged_queue.exists():
        staged = read_queue(paths.staged_queue)
        if staged:
            urls += staged
            out(f"[{stamp()}] {len(staged)} staged URL(s) from queue-requeue.txt added to this run.")
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
        out(f"[{stamp()}] archive: {merged.total} row(s) left pending by the last run were merged.")

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
        out(f"[{stamp()}] {plan.unusable} unusable queue line(s) dropped (not an http(s) URL).")
    if plan.skipped:
        out(f"[{stamp()}] {plan.summary()}")
        if not settings.dry_run:
            records.write_lines(paths.queue, plan.urls)

    ordered, non_porn, porn = order_by_destination(
        plan.urls, settings=settings, domains=domains
    )
    if non_porn and porn:
        out(f"[{stamp()}] {non_porn} non-porn link(s) first, then {porn} adult link(s).")
    if not ordered:
        return None

    out("dredge v8 - multi-engine downloader")
    out(f"[{stamp()}] {len(ordered)} links queued.")

    stats = PassStats(total=len(ordered))
    stats.log = paths.logs / time.strftime("run-%Y%m%d-%H%M%S.log")
    _log_line(stats.log, f"[{stats.started:%Y-%m-%d %H:%M:%S}] dredge run - {len(ordered)} links queued")
    out(f"[{stamp()}] Run log: {stats.log}")

    remaining = list(ordered)
    pending = list(range(len(ordered)))
    active: dict[int, Lane] = {}
    done = 0
    pending_rows = 0
    pending_since = time.monotonic()
    space_at = 0.0
    progress_at = time.monotonic()
    progress_last: dict[int, str] = {}
    queued_canon = {knowledge.canon(u) for u in ordered}

    while pending or active:
        while len(active) < runtime.max_downloads and pending:
            if not settings.dry_run and (time.monotonic() - space_at) >= 120:
                space_at = time.monotonic()
                free = free_gb(settings.dest_porn)
                if 0 <= free < SPACE_STOP_GB:
                    message = (
                        f"[{stamp()}] Stopping: only {free:.0f} GB free on "
                        f"{Path(settings.dest_porn).anchor} - {len(pending)} link(s) stay queued."
                    )
                    out(message)
                    _log_line(stats.log, message)
                    pending.clear()
                    break
            index = _take_next(pending, ordered, active, knowledge)
            if index < 0:
                break
            slot = _free_slot(active, runtime.max_downloads)
            if slot is None:
                break
            url = ordered[index]
            dest = destination(url, settings, domains)
            lane_log = paths.lane_log(slot)
            lane_result = paths.lane_result(slot)
            lane_archive = paths.lane_archive(slot)
            for stale in (lane_result, lane_archive):
                try:
                    stale.unlink()
                except OSError:
                    pass
            worker = Worker(
                paths, settings, engines, knowledge,
                domains=domains, filehosts=filehosts,
                strict_generic=runtime.strict_generic,
            )
            thread = threading.Thread(
                target=_run_worker,
                args=(worker, url, dest, dest == settings.dest_porn, not runtime.archive_check,
                      lane_log, lane_result, lane_archive, slot),
                daemon=True,
            )
            active[slot] = Lane(slot, url, thread, lane_result, lane_archive)
            thread.start()

        _sleep(0.5, runtime, knowledge)
        now = time.monotonic()

        if settings.progress_every_seconds > 0 and (now - progress_at) >= settings.progress_every_seconds:
            progress_at = now
            for slot in sorted(active):
                name, pct = lane_status(paths.lane_log(slot))
                if not name or progress_last.get(slot) == name:
                    continue
                progress_last[slot] = name
                detail = f"{name}  ({pct})" if pct else name
                out(f"[{stamp()}] lane {slot}: {detail}")

        if not settings.no_capture and not settings.dry_run:
            captured = state.read_clip_inbox(paths.clip_inbox)
            if captured:
                added = _capture(captured, queued_canon, runtime, knowledge, settings)
                if added:
                    records.append_lines(paths.queue, added)
                    for url in added:
                        if url not in remaining:
                            remaining.append(url)
                            ordered.append(url)
                            pending.append(len(ordered) - 1)

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
                            out(f"[{stamp()}] archive: {merged_now.total} row(s) merged.")

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
            _log_line(stats.log, f"[{stamp()}] {verdict}{files}  {lane.url}")
            done += 1
            out(f"[{stamp()}] Progress: {done}/{len(ordered)} done.")

    if not settings.dry_run:
        merged_end = merge_pending(
            paths.fold_pending, paths.archive, runtime.seen, runtime.archived, knowledge
        )
        runtime.arc_sig = state.archive_signature(paths.archive)
        if merged_end.total:
            out(f"[{stamp()}] archive: {merged_end.total} row(s) merged.")

    end = _local_now()
    elapsed = end - stats.started
    _log_line(
        stats.log,
        f"[{end:%Y-%m-%d %H:%M:%S}] Finished in {elapsed}. OK={stats.ok_total} "
        f"[{stats.engine_summary()}] FAILED={stats.failed} OFFLINE={stats.offline}",
    )
    out(f"[{stamp()}] Finished.")
    out(f"Run log: {stats.log}")
    return stats


def _run_worker(
    worker: Worker,
    url: str,
    dest: Path,
    is_porn: bool,
    ignore: bool,
    lane_log: Path,
    result: Path,
    archive: Path,
    slot: int,
) -> None:
    try:
        worker.download(
            url, dest=dest, is_porn_dest=is_porn, ignore_records=ignore,
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
    if len(rows) == 1:
        fields = rows[0].split("\t")
        return f" -> {fields[2]}" if len(fields) >= 3 and fields[2] else ""
    if len(rows) > 1:
        count = sum(
            len([n for n in row.split("\t")[2].split(";") if n])
            for row in rows if len(row.split("\t")) >= 3
        )
        return f" -> {len(rows)} pages, {count} files"
    return ""


def _warn_pages(rows: list[str], out, stamp) -> None:
    for row in rows:
        fields = row.split("\t")
        if len(fields) >= 3 and fields[2]:
            count = len([n for n in fields[2].split(";") if n])
            if count > 8:
                out(
                    f"[{stamp()}] WARNING: one URL produced {count} files (looks like a page "
                    f"listing, not a single video): {fields[1]}"
                )


def _mark_failed(runtime: Runtime, knowledge: Knowledge, url: str) -> None:
    c = knowledge.canon(url)
    if not c:
        return
    runtime.failed.add(c)
    ident = knowledge.seen_id(c)
    if ident:
        runtime.failed.add(ident)


def _capture(
    urls: list[str],
    queued: set[str],
    runtime: Runtime,
    knowledge: Knowledge,
    settings,
) -> list[str]:
    added: list[str] = []
    for url in urls:
        c = knowledge.canon(url)
        if not c or c in queued:
            continue
        ident = knowledge.seen_id(c)
        if runtime.archive_check and (c in runtime.seen or (ident and ident in runtime.seen)):
            continue
        if runtime.archive_check and (
            c in runtime.skip_always or url in runtime.skip_always
            or (ident and ident in runtime.skip_always)
        ):
            continue
        if runtime.archive_check and not settings.retry_failed and (
            c in runtime.failed or url in runtime.failed or (ident and ident in runtime.failed)
        ):
            continue
        queued.add(c)
        added.append(url)
    return added


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


def _sleep(seconds: float, runtime: Runtime, knowledge: Knowledge) -> None:
    from . import menu
    from .tui import show_dredge_menu

    menu.wait(seconds, lambda: show_dredge_menu(runtime, knowledge))
