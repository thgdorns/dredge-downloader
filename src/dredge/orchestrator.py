"""The pass loop: take the queue, run the lanes, fold the rows, come back.

Threads run the worker in this process. The legacy script spawned a child
PowerShell per URL; a thread is enough here because every engine call is a
subprocess and the orchestrator only polls, so a slow engine cannot block it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paint, records, state
from .archive import append_pending, merge_pending
from .config import Paths, Runtime, Settings
from .destinations import (
    apply_fallback,
    destination_for,
    destination_override,
    free_gb,
    load_domains,
)
from .engines import Engines, lane_status, lane_status_tail
from .lanes import LaneBoard
from .plan import build_plan, order_by_destination, read_queue, split_line
from .urls import Knowledge, host
from .worker import Worker

SPACE_STOP_GB = 10
# How often the free-space guard re-checks a destination already in use. The
# lane-start check cannot see a disk that fills while its own lanes run.
SPACE_RECHECK_SECONDS = 120


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
    line: str = ""          # the queue line: the URL plus any tab destination


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


def _line(stamp, message: str, color: str = "") -> str:
    """A terminal line: blue ``[HH:MM:SS]`` then the message in ``color``."""
    body = paint.paint(message, color) if color else message
    return f"{paint.blue(f'[{stamp()}]')} {body}"


def _authorize(engines: Engines, site: str, paths: Paths) -> bool:
    """Run gallery-dl's ``oauth:<site>`` flow so it caches a token.

    Interactive by design: gallery-dl prints a URL and then asks for the
    ``code`` from the browser, so the child inherits the console. gallery-dl
    writes the token to its own cache, so nothing is copied into the config.
    """
    exe = engines.gdl
    if not exe:
        return False
    cmd = [exe, "--config-ignore", "-c", str(paths.gallery_conf)]
    if paths.gallery_runtime.exists():
        cmd += ["-c", str(paths.gallery_runtime)]
    cmd.append(f"oauth:{site}")
    try:
        return subprocess.call(cmd, cwd=str(paths.root)) == 0
    except OSError:
        return False


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
    out(_line(stamp, f"{refreshed.fallback_note} ({path}).", "yellow"))
    return refreshed


def drain_clipboard(paths: Paths, settings: Settings) -> list[str]:
    """Move clipboard URLs from ``clip-inbox.txt`` into ``queue.txt``.

    The Capture thread only writes the inbox. Draining it is separate from
    downloading, so the paused loop can keep queue.txt current without starting
    a pass. Returns the lines added.
    """
    if settings.dry_run or settings.no_capture:
        return []
    captured = state.read_clip_inbox(paths.clip_inbox)
    if not captured:
        return []
    # One copy can update the clipboard more than once (different text flavours
    # hash differently), so the same URL can arrive twice; keep one copy of each
    # and skip whatever the queue already holds. Draining never touches the
    # paused flag: a pause holds the queue, it does not stop it growing.
    known = set(records.read_lines(paths.queue))
    fresh = [line for line in dict.fromkeys(captured) if line not in known]
    if fresh:
        records.append_lines(paths.queue, fresh)
    return fresh


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

    captured = drain_clipboard(paths, settings)
    if captured:
        out(_line(stamp, f"clipboard: +{len(captured)} URL(s) queued.", "cyan"))
        urls += captured

    if not settings.dry_run and paths.staged_queue.exists():
        staged = read_queue(paths.staged_queue)
        fresh = [url for url in staged if url not in urls]
        if fresh:
            # An expanded listing belongs in queue.txt like any other link, so
            # the queue shows it while it runs and a restart resumes it.
            records.append_lines(paths.queue, fresh)
            urls += fresh
            out(_line(
                stamp,
                f"{len(fresh)} staged URL(s) from queue-requeue.txt added to this run.",
                "darkgray",
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
        out(_line(
            stamp,
            f"archive: {merged.total} row(s) left pending by the last run were merged.",
            "darkgray",
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
        out(_line(
            stamp,
            f"{plan.unusable} unusable queue line(s) dropped (not an http(s) URL).",
            "yellow",
        ))
    if plan.skipped:
        out(_line(stamp, plan.summary(), "darkgray"))
        if not settings.dry_run:
            records.write_lines(paths.queue, plan.urls)

    ordered, primary, secondary = order_by_destination(
        plan.urls, settings=settings, knowledge=knowledge
    )
    if primary and secondary:
        out(_line(
            stamp,
            f"{primary} link(s) to the default destination first, "
            f"then {secondary} to the others.",
            "darkgray",
        ))
    if not ordered:
        return None

    out(paint.cyan("dredge v8 - multi-engine downloader"))
    out(_line(stamp, f"{len(ordered)} links queued."))

    stats = PassStats(total=len(ordered))
    stats.log = paths.logs / time.strftime("run-%Y%m%d-%H%M%S.log")
    _log_line(stats.log, f"[{stats.started:%Y-%m-%d %H:%M:%S}] dredge run - {len(ordered)} links queued")
    out(_line(stamp, f"Run log: {stats.log}", "darkgray"))

    remaining = list(ordered)
    pending = list(range(len(ordered)))
    active: dict[int, Lane] = {}
    done = 0
    pending_rows = 0
    pending_since = time.monotonic()
    space_checked: dict[str, float] = {}
    full_dests: set[str] = set()
    auth_tried: set[str] = set()
    quota_until: dict[str, float] = {}
    # The lane-start check below only runs when a link is dispatched. A pass
    # whose links were all dispatched while the disk had room fills it and then
    # sits with nothing pending, so the check never fires again: the pass hangs
    # on lanes that cannot write, and the fallback drive is never used. Track
    # the destinations in use and re-check them on a timer as well.
    dest_roots: set[str] = set()
    space_at = time.monotonic()
    progress_at = time.monotonic()
    progress_last: dict[int, tuple[str, bool]] = {}
    queued_canon = {key for u in ordered for key in knowledge.keys(split_line(u)[0])}

    board = _make_board(out)
    if board:
        board.start()
    try:
        while pending or active:
            if paths.paused.exists() and not active:
                break           # leave the rest in queue.txt
            if quota_until:
                now_quota = time.time()
                for name in [h for h, until in quota_until.items() if until <= now_quota]:
                    del quota_until[name]
                    out(_line(
                        stamp,
                        f"{name}: quota window over - trying its links again.",
                        "green",
                    ))
            if not settings.dry_run and dest_roots:
                now_space = time.monotonic()
                if now_space - space_at >= SPACE_RECHECK_SECONDS:
                    space_at = now_space
                    low = sorted(
                        root for root in dest_roots
                        if 0 <= free_gb(root) < SPACE_STOP_GB
                    )
                    fresh = [root for root in low if root not in full_dests]
                    if fresh:
                        root = fresh[0]
                        full_dests.add(root)
                        free = free_gb(root)
                        message = (
                            f"Stopping: only {free:.0f} GB free on "
                            f"{Path(root).anchor} - its links stay queued."
                        )
                        out(_line(stamp, message, "yellow"))
                        _log_line(stats.log, f"[{stamp()}] {message}")
                        refreshed = _fallback(runtime, settings, out, stamp)
                        if refreshed is not settings:
                            settings = refreshed
                            full_dests.clear()
                            space_checked.clear()
                            dest_roots.clear()
            queued_before = len(pending)
            while len(active) < runtime.max_downloads and pending:
                if paths.paused.exists():
                    break       # let running lanes finish, start nothing new
                index = _take_next(pending, ordered, active, knowledge, quota_until)
                if index < 0:
                    break
                line = ordered[index]
                url, spec = split_line(line)
                target = destination_for(url, knowledge, settings)
                override = destination_override(spec, target.path) if spec else None
                dest_root = override or target.path
                dest_roots.add(str(dest_root).lower())
                # A queue line's destination is handed to the engine as a full
                # filepath template, so the engine writes there and nothing is
                # added underneath; gallery-dl gets the folder plus -D.
                dest_tpl = (
                    str(override / settings.tpl_flat) if override else target.tpl
                )
                if not settings.dry_run:
                    if str(dest_root).lower() in full_dests:
                        continue        # leave it in queue.txt; this pass skips it
                    last = space_checked.get(str(dest_root).lower(), 0.0)
                    if (time.monotonic() - last) >= SPACE_RECHECK_SECONDS:
                        space_checked[str(dest_root).lower()] = time.monotonic()
                        free = free_gb(dest_root)
                        if 0 <= free < SPACE_STOP_GB:
                            full_dests.add(str(dest_root).lower())
                            message = (
                                f"Stopping: only {free:.0f} GB free on "
                                f"{dest_root.anchor} ({target.name}) - its links stay queued."
                            )
                            out(_line(stamp, message, "yellow"))
                            _log_line(stats.log, f"[{stamp()}] {message}")
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
                    shorteners=load_domains(paths.shorteners),
                    strict_generic=runtime.strict_generic,
                )
                thread = threading.Thread(
                    target=_run_worker,
                    args=(worker, url, dest_root, target.layout == "flat" or bool(override),
                          dest_tpl,
                          not runtime.archive_check, lane_log, lane_result, lane_archive, slot),
                    kwargs={"spec": spec},
                    daemon=True,
                )
                active[slot] = Lane(slot, url, thread, lane_result, lane_archive,
                                    index + 1, line)
                thread.start()
                if board:
                    board.set_url(slot, index + 1, len(ordered), url)

            if pending and not active and len(pending) == queued_before and quota_until:
                # Everything left belongs to a host parked for its quota window:
                # idle until the earliest one expires instead of re-dispatching
                # twice a second for the whole window.
                left = min(quota_until.values()) - time.time()
                _sleep(max(1.0, min(30.0, left)), runtime, knowledge, board)

            _sleep(0.5, runtime, knowledge, board)
            picked = _catch_up_queue(
                paths, knowledge, ordered, pending, remaining, queued_canon, settings
            )
            if picked:
                out(_line(stamp, f"queue: +{len(picked)} URL(s) picked up.", "cyan"))
            now = time.monotonic()

            if board:
                board.ensure(range(1, runtime.max_downloads + 1), active)
                board.set_paused(paths.paused.exists(), len(active))
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
                    out(_line(
                        stamp,
                        f"{lane.index}/{len(ordered)}: lane {slot}: {detail}",
                        "darkgray",
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
                        out(_line(
                            stamp,
                            f"clipboard: +{len(added)} URL(s) queued.",
                            "cyan",
                        ))
                    if rejected:
                        total = sum(rejected.values())
                        parts = ", ".join(
                            f"{count} {reason}" for reason, count in rejected.items()
                        )
                        out(_line(
                            stamp,
                            f"clipboard: {total} URL(s) skipped ({parts}).",
                            "darkgray",
                        ))
                        _log_line(
                            stats.log,
                            f"[{stamp()}] clipboard: {total} URL(s) skipped ({parts}).",
                        )

            finished = [slot for slot, lane in active.items() if not lane.thread.is_alive()]
            for slot in finished:
                lane = active.pop(slot)
                progress_last.pop(slot, None)
                verdict = _verdict(lane.result)
                rows = _archive_rows(lane.archive)
                if rows:
                    _warn_pages(rows, out, stamp, settings)
                    if not settings.dry_run:
                        pending_rows += append_pending(paths.fold_pending, rows)
                        if (
                            pending_rows >= settings.fold_batch_rows
                            or (time.monotonic() - pending_since) >= settings.fold_batch_seconds
                        ):
                            merged_now = _fold(paths, runtime, knowledge, out, stamp)
                            if merged_now is not None:
                                pending_rows = 0
                                pending_since = time.monotonic()
                                if merged_now:
                                    out(_line(
                                        stamp,
                                        f"archive: {merged_now} row(s) merged.",
                                        "darkgray",
                                    ))

                record_line = lane.line or lane.url
                if record_line in remaining:
                    remaining.remove(record_line)
                if not settings.dry_run:
                    _save_queue(paths, remaining, ordered)
                _settle(queued_canon, runtime, knowledge, record_line, verdict, rows)
                if verdict.startswith("AUTH|"):
                    # The site is pinned to gallery-dl and it has no token yet.
                    # Run its login flow now (interactive), then retry the link:
                    # once per site per pass, so a cancelled flow cannot loop.
                    site = verdict[5:]
                    if site not in auth_tried:
                        auth_tried.add(site)
                        out(_line(
                            stamp,
                            f"{site}: authorization needed - starting gallery-dl's "
                            "login flow (it prints a URL, then asks for a code)",
                            "yellow",
                        ))
                        if board:
                            board.stop()
                        try:
                            authorized = _authorize(engines, site, paths)
                        finally:
                            if board:
                                board.start()
                        if authorized:
                            out(_line(stamp, f"{site}: authorized - retrying.", "green"))
                        else:
                            out(_line(
                                stamp,
                                f"{site}: login flow did not finish - the link is "
                                "retried once, then recorded as failed.",
                                "yellow",
                            ))
                        remaining.append(record_line)
                        # The retry is queued again, so the clipboard check must
                        # see it as waiting; otherwise the queue catch-up treats
                        # this line in queue.txt as a new link and schedules it
                        # a second time on every pass around the loop.
                        queued_canon.update(knowledge.keys(split_line(record_line)[0]))
                        if not settings.dry_run:
                            _save_queue(paths, remaining, ordered)
                        pending.append(lane.index - 1)   # lane.index is 1-based
                        continue
                    records.append_lines(paths.fail, [record_line])
                    stats.failed += 1
                    _mark_failed(runtime, knowledge, lane.url)
                elif verdict.startswith("QUOTA|"):
                    # The quota is per IP, so one answer applies to every link on
                    # the host. Park them for the window instead of failing each.
                    blocked = verdict[6:]
                    records.append_lines(paths.fail, [record_line])
                    stats.failed += 1
                    _mark_failed(runtime, knowledge, lane.url)
                    if settings.quota_block_minutes > 0:
                        until = time.time() + settings.quota_block_minutes * 60
                        quota_until[blocked] = max(
                            quota_until.get(blocked, 0.0), until
                        )
                        out(_line(
                            stamp,
                            f"{blocked}: out of quota - its links wait until "
                            f"{time.strftime('%H:%M', time.localtime(until))} "
                            f"(quota_block_minutes = {settings.quota_block_minutes}).",
                            "yellow",
                        ))
                elif verdict == "OFFLINE":
                    records.append_lines(paths.offline, [record_line])
                    stats.offline += 1
                    _mark_failed(runtime, knowledge, lane.url)
                elif not verdict.startswith("OK|"):
                    records.append_lines(paths.fail, [record_line])
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
                url_text = (
                    paint.lightgreen(lane.url)
                    if verdict.startswith("OK|")
                    else paint.lightred(lane.url)
                )
                out(_line(
                    stamp,
                    f"{done}/{len(ordered)}: {label}: {verdict}  {url_text}{shown}",
                ))
                if board:
                    board.set_done(
                        slot, lane.index, len(ordered), verdict, files,
                        folder=folder_uri, target=target_uri,
                    )

        if not settings.dry_run:
            merged_end = _fold(paths, runtime, knowledge, out, stamp)
            if merged_end:
                out(_line(
                    stamp,
                    f"archive: {merged_end} row(s) merged.",
                    "darkgray",
                ))

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
    out(_line(stamp, "Finished.", "green"))
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
    *,
    spec: str = "",
) -> None:
    try:
        worker.download(
            url, dest=dest, flat=flat, dest_tpl=dest_tpl, ignore_records=ignore,
            lane_log=lane_log, result=result, archive=archive, slot=slot,
            queue_spec=spec,
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


def _read_path(path: Path, tail: int = 4096) -> Path | None:
    """The last path a lane published (its target file or its destination).

    laneN.target grows one line per file in a crawl, and the last line is the
    file being written now; only the tail is read so a 100k-line file costs
    nothing on every refresh.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    if not lines:
        return None
    return Path(lines[-1])


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


def _warn_pages(rows: list[str], out, stamp, settings: Settings) -> None:
    """Warn when one URL wrote a pile of files - usually a page listing a rule
    mistook for a single post. ``warn_page_listing = off`` silences it."""
    if settings.warn_page_listing.strip().lower() in ("off", "no", "false", "0"):
        return
    for row in rows:
        fields = row.split("\t")
        if len(fields) >= 3 and fields[2]:
            count = len([n for n in fields[2].split(";") if n])
            if count > 8:
                out(_line(
                    stamp,
                    f"WARNING: one URL produced {count} files (looks like a page "
                    f"listing, not a single video): {fields[1]}",
                    "yellow",
                ))


def _settle(
    queued: set[str],
    runtime: Runtime,
    knowledge: Knowledge,
    line: str,
    verdict: str,
    rows: list[str],
) -> None:
    """A finished link leaves the queue; a successful one counts as downloaded.

    ``queued`` is what the clipboard check consults. Leaving a finished link in
    it made the next paste of that URL answer "already queued" for the rest of
    the pass, long after it had left queue.txt - and while it was in fact
    downloaded or failed.
    """
    url = split_line(line)[0]
    keys = knowledge.keys(url)
    queued.difference_update(keys)
    if not verdict.startswith("OK|") or not rows:
        return
    runtime.seen.update(keys)
    ident = knowledge.seen_id(url)
    if ident:
        runtime.seen.add(ident)


def _mark_failed(runtime: Runtime, knowledge: Knowledge, url: str) -> None:
    keys = knowledge.keys(url)
    if not keys:
        return
    runtime.failed.update(keys)
    ident = knowledge.seen_id(keys[0])
    if ident:
        runtime.failed.add(ident)


def _fold(
    paths: Paths,
    runtime: Runtime,
    knowledge: Knowledge,
    out,
    stamp,
) -> int | None:
    """Merge parked rows into downloads.tsv; ``None`` when it is locked.

    The merge replaces downloads.tsv atomically, which needs delete access to
    the old file. An editor holding it open (the files menu opens it in
    Notepad) refuses that, and the exception used to end the whole pass. The
    rows stay in fold-pending.tsv, so the next merge picks them up.
    """
    try:
        merged = merge_pending(
            paths.fold_pending, paths.archive, runtime.seen, runtime.archived, knowledge
        )
    except OSError as exc:
        out(_line(
            stamp,
            "archive: downloads.tsv could not be written "
            f"({exc.strerror or exc}) - its rows are kept for the next merge.",
            "yellow",
        ))
        return None
    runtime.arc_sig = state.archive_signature(paths.archive)
    return merged.total


def _save_queue(paths: Paths, remaining: list[str], ordered: list[str]) -> None:
    """Rewrite queue.txt from ``remaining`` without dropping external edits.

    The menu's retry-failed action appends to queue.txt while a pass runs, and
    this rewrite happens on every lane completion - before the catch-up read
    that would adopt those lines. Writing ``remaining`` alone deleted links
    added in that window: they were gone from queue.txt, and fail.txt had
    already been emptied. Keep whatever is on disk that this pass never planned
    (``ordered``), so the next catch-up adopts it; finished links are not
    resurrected because they are still in ``ordered``.
    """
    known = set(ordered)
    try:
        on_disk = records.read_lines(paths.queue)
    except OSError:
        on_disk = []
    extra = [line for line in on_disk if line not in known]
    records.write_lines(paths.queue, remaining + extra if extra else remaining)


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
            keys = knowledge.keys(split_line(url)[0])
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
        keys = knowledge.keys(split_line(url)[0])
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

    for line in urls:
        url, _ = split_line(line)
        if not url.lower().startswith(("http://", "https://")):
            reject("not a URL")
            continue
        keys = knowledge.keys(url)
        if not keys:
            reject("not a URL")
            continue
        if url in queued or any(key in queued for key in keys):
            # Already scheduled: the same copy can reach the clipboard twice
            # (different text flavours), and a link can be pasted again while it
            # waits. Nothing is lost and nothing can be done, so this is not a
            # rejection - reporting it as "already queued" made every copy look
            # as if Dredge had refused the link.
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
        added.append(line)
    return added, rejected


def _serial(url: str, knowledge: Knowledge) -> bool:
    rule = knowledge.rule(url)
    return bool(rule and rule.serial)


def _take_next(
    pending: list[int],
    urls: list[str],
    active: dict[int, Lane],
    knowledge: Knowledge,
    blocked: dict[str, float] | None = None,
) -> int:
    busy = {
        host(lane.url)
        for lane in active.values()
        if _serial(lane.url, knowledge)
    }
    now = time.time()
    for pos, index in enumerate(pending):
        name = host(split_line(urls[index])[0])
        if name in busy:
            continue
        if blocked and blocked.get(name, 0.0) > now:
            continue        # its quota window is still open
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
    from .tui import show_dredge_menu, toggle_paused

    def open_menu() -> None:
        if board:
            board.stop()
        try:
            show_dredge_menu(runtime, knowledge)
        finally:
            if board:
                board.start()

    def toggle() -> None:
        stamp_text = paint.blue(f"[{time.strftime('%H:%M:%S')}]")
        try:
            paused, message = toggle_paused(runtime, knowledge)
        except OSError as exc:
            say(
                f"{stamp_text} {paint.red(f'pause failed: {exc}')}",
                runtime.paths,
            )
            return
        color = paint.yellow if paused else paint.green
        # Print above the live block instead of stopping it: Live.stop() writes
        # a newline and then erases its height, which overshoots and clears the
        # lines the run printed above the lanes.
        say(f"{stamp_text} {color(message)}", runtime.paths)

    menu.wait(seconds, open_menu, on_pause=toggle)
