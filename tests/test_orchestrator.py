import threading
import time
from pathlib import Path

import pytest

from dredge import orchestrator, records
from dredge.config import Paths, Runtime, Settings
from dredge.orchestrator import (
    Lane,
    _capture,
    _catch_up_queue,
    _fallback,
    _files_summary,
    _fold,
    _free_slot,
    _mark_failed,
    _settle,
    _take_next,
    run_pass,
)
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


@pytest.fixture(autouse=True)
def _plenty_of_space(monkeypatch):
    """Keep the pass-loop tests independent of the runner's free disk space."""
    monkeypatch.setattr(orchestrator, "free_gb", lambda path: 100.0)
    monkeypatch.setattr(orchestrator, "apply_fallback", lambda settings: settings)


def test_fallback_rechecks_drive_space(tmp_path, monkeypatch):
    from dredge import destinations

    settings = Settings.load(
        Paths(tmp_path),
        dest_default=Path("D:/Downloads"),
        dest_fallback=("G:",),
        dest_fallback_below_gb=20,
    )
    runtime = Runtime(Paths(tmp_path), settings)
    monkeypatch.setattr(
        destinations,
        "free_gb",
        lambda path: 5.0 if str(path).upper().startswith("D:") else 100.0,
    )
    monkeypatch.setattr(orchestrator, "apply_fallback", destinations.apply_fallback)

    messages: list[str] = []
    refreshed = _fallback(runtime, settings, messages.append, lambda: "12:00:00")

    assert refreshed is not settings
    assert str(refreshed.destinations["default"].path).upper().startswith("G:")
    assert runtime.settings is refreshed
    assert any("writing to G:" in message for message in messages)


def test_the_space_guard_reruns_while_lanes_are_busy(tmp_path, monkeypatch):
    """A pass that fills the disk itself must still notice and switch drives.

    The lane-start check only runs when a link is dispatched. When the queue's
    remaining links were all dispatched while there was room, the disk fills
    during the pass and nothing ever checks again - the pass then hangs on
    lanes that cannot write and the fallback drive is never used.
    """
    import time as _time

    from dredge import destinations

    free = {"gb": 100.0}

    class FillingWorker:
        """Stands in for the engine: the disk drops below the guard mid-lane."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url, *, dest, flat, dest_tpl, ignore_records,
                     lane_log, result, archive, slot, **kwargs):
            free["gb"] = 0.5          # the lane itself filled the disk
            _time.sleep(1.5)
            Path(result).write_text("OK|gallery-dl", encoding="utf-8")
            return "OK|gallery-dl"

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
        dest_fallback=("G:",), dest_fallback_below_gb=20,
    )
    records.write_lines(paths.queue, ["https://example.com/slow"])

    def space(path) -> float:
        first = str(path)[0].upper()
        return free["gb"] if first == str(tmp_path)[0].upper() else 100.0

    monkeypatch.setattr(orchestrator, "Worker", FillingWorker)
    monkeypatch.setattr(orchestrator, "SPACE_RECHECK_SECONDS", 0)
    monkeypatch.setattr(orchestrator, "free_gb", space)
    monkeypatch.setattr(orchestrator, "apply_fallback", destinations.apply_fallback)
    monkeypatch.setattr(destinations, "free_gb", space)

    runtime = Runtime(paths, settings)
    messages: list[str] = []
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=messages.append,
    )

    assert any("Stopping: only 0 GB free" in message for message in messages)
    assert str(runtime.settings.destinations["default"].path)[0].upper() == "G"


def test_an_auth_verdict_runs_the_login_flow_and_retries_once(tmp_path, monkeypatch):
    """AUTH|site stops the board, runs the flow, and retries the same link."""
    calls: list[str] = []

    class AuthWorker:
        runs = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url, *, result, **kwargs):
            AuthWorker.runs += 1
            verdict = "AUTH|pixiv" if AuthWorker.runs == 1 else "OK|gallery-dl"
            Path(result).write_text(verdict, encoding="utf-8")
            return verdict

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://www.pixiv.net/artworks/1"])
    monkeypatch.setattr(orchestrator, "Worker", AuthWorker)
    board = FakeBoard()

    def fake_authorize(engines, site, p):
        calls.append(site)
        # the pass starts the board once, so equal counters = stopped right now
        assert board.stopped >= board.started
        return True

    monkeypatch.setattr(orchestrator, "_authorize", fake_authorize)
    monkeypatch.setattr(orchestrator, "_make_board", lambda out: board)

    runtime = Runtime(paths, settings)
    messages: list[str] = []
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=messages.append,
    )

    assert calls == ["pixiv"]          # the flow ran exactly once
    assert AuthWorker.runs == 2        # the link was retried
    assert any("authorized" in message for message in messages)
    assert not paths.fail.exists()     # the retry succeeded, so nothing failed


def test_a_second_auth_verdict_for_the_same_site_fails_without_a_flow(
    tmp_path, monkeypatch
):
    """A cancelled flow must not loop: the retry is the only second chance."""
    calls: list[str] = []

    class AlwaysAuthWorker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url, *, result, **kwargs):
            Path(result).write_text("AUTH|pixiv", encoding="utf-8")
            return "AUTH|pixiv"

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://www.pixiv.net/artworks/1"])
    monkeypatch.setattr(orchestrator, "Worker", AlwaysAuthWorker)
    monkeypatch.setattr(
        orchestrator, "_authorize",
        lambda engines, site, p: (calls.append(site), False)[1],
    )
    monkeypatch.setattr(orchestrator, "_make_board", lambda out: None)

    runtime = Runtime(paths, settings)
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert calls == ["pixiv"]                      # offered only once
    assert paths.fail.exists()                     # the retry was recorded
    assert "artworks/1" in paths.fail.read_text(encoding="utf-8")


def test_a_link_appended_mid_pass_survives_the_queue_rewrite(
    tmp_path, monkeypatch
):
    """Menu 1->2 appends to queue.txt while lanes run.

    The rewrite after every lane completion used to happen before the catch-up
    read, so a link added in that window was written out of queue.txt and never
    scheduled - gone from both queue.txt and fail.txt (which retry-failed had
    already emptied).
    """
    seen: list[str] = []

    class AppendingWorker:
        appended = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url, *, result, **kwargs):
            seen.append(url)
            if not AppendingWorker.appended:
                AppendingWorker.appended = True
                records.append_lines(paths.queue, ["https://example.com/second"])
            Path(result).write_text("OK|gallery-dl", encoding="utf-8")
            return "OK|gallery-dl"

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/first"])
    monkeypatch.setattr(orchestrator, "Worker", AppendingWorker)
    monkeypatch.setattr(orchestrator, "_make_board", lambda out: None)

    run_pass(
        Runtime(paths, settings),
        Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert seen == ["https://example.com/first", "https://example.com/second"]
    assert "second" not in paths.queue.read_text(encoding="utf-8")


def test_the_page_listing_warning_can_be_turned_off(tmp_path):
    """A pixiv gallery legitimately writes a dozen files for one URL."""
    names = ";".join(f"file{i}.jpg" for i in range(12))
    rows = [f"2026-11-07 12:00:00\tsite.example/post/1\t{names}"]
    paths = Paths(tmp_path)

    on = Settings.load(paths)
    off = Settings.load(paths, warn_page_listing="off")

    messages: list[str] = []
    orchestrator._warn_pages(rows, messages.append, lambda: "12:00:00", on)
    assert any("looks like a page listing" in message for message in messages)

    messages.clear()
    orchestrator._warn_pages(rows, messages.append, lambda: "12:00:00", off)
    assert messages == []


def test_the_page_listing_setting_is_read_from_the_config_file(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "dredge.conf").write_text(
        "warn_page_listing = off\n", encoding="utf-8"
    )

    assert Settings.load(paths).warn_page_listing == "off"


def test_one_quota_answer_parks_the_whole_host(tmp_path, monkeypatch):
    """MEGA's quota is per IP, so one 509 means the rest would fail too.

    The pass must stop dispatching that host instead of burning every link into
    fail.txt, and leave them queued for when the window ends.
    """
    seen: list[str] = []

    class QuotaWorker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url, *, result, **kwargs):
            seen.append(url)
            Path(result).write_text("QUOTA|mega.nz", encoding="utf-8")
            paths.paused.touch()      # ends the pass so the test can finish
            return "QUOTA|mega.nz"

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, [
        "https://mega.nz/file/first#key",
        "https://mega.nz/file/second#key",
    ])
    monkeypatch.setattr(orchestrator, "Worker", QuotaWorker)
    monkeypatch.setattr(orchestrator, "_make_board", lambda out: None)

    messages: list[str] = []
    run_pass(
        Runtime(paths, settings),
        Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=messages.append,
    )

    assert seen == ["https://mega.nz/file/first#key"]    # the second never ran
    assert any("out of quota" in message for message in messages)
    assert "file/second" in paths.queue.read_text(encoding="utf-8")
    assert "file/first" in paths.fail.read_text(encoding="utf-8")
    paths.paused.unlink(missing_ok=True)


def test_zero_lanes_are_clamped_to_one(tmp_path):
    settings = Settings.load(Paths(tmp_path), max_downloads=0)
    runtime = Runtime(Paths(tmp_path), settings)
    assert runtime.max_downloads == 1


def test_free_slot_refuses_a_zero_lane_pool():
    assert _free_slot({}, 0) is None
    assert _free_slot({}, 2) == 1
    assert _free_slot({1: None, 2: None}, 2) is None


def serial_knowledge() -> Knowledge:
    return Knowledge(
        rules={"example.com": parse_rule("example.com serial")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )


def lane(url: str) -> Lane:
    return Lane(
        slot=1,
        url=url,
        thread=threading.Thread(),
        result=Path("lane1.result"),
        archive=Path("lane1.archive"),
    )


def test_a_serial_host_is_not_crawled_by_two_lanes():
    urls = ["https://example.com/b", "https://other.example/c"]
    pending = [0, 1]
    active = {1: lane("https://example.com/a")}
    index = _take_next(pending, urls, active, serial_knowledge())
    assert index == 1
    assert pending == [0]


def test_a_non_serial_host_keeps_the_queue_order():
    urls = ["https://example.com/b", "https://other.example/c"]
    pending = [0, 1]
    active = {1: lane("https://third.example/a")}
    index = _take_next(pending, urls, active, serial_knowledge())
    assert index == 0


class FakeWorker:
    """Stand-in for the real worker: one verdict, one row, no engines."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def download(self, url, *, dest, flat, dest_tpl, ignore_records, lane_log,
                 result, archive, slot, **kwargs):
        Path(lane_log).write_text(f"[test] {url}\r\n", encoding="utf-8")
        if "bad" in url:
            Path(result).write_text("FAILED", encoding="utf-8")
            return "FAILED"
        Path(result).write_text("OK|gallery-dl", encoding="utf-8")
        Path(archive).write_text(
            f"2026-01-01 00:00:00\turl:{url}\tclip.mp4\t200000\r\n",
            encoding="utf-8",
        )
        return "OK|gallery-dl"


def test_a_pass_records_success_and_failure(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, [
        "https://example.com/good",
        "https://example.com/bad",
    ])
    monkeypatch.setattr(orchestrator, "Worker", FakeWorker)

    runtime = Runtime(paths, settings)
    messages: list[str] = []
    stats = run_pass(runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
                     orchestrator.Engines(), out=messages.append)

    assert stats is not None
    assert stats.ok == {"gallery-dl": 1}
    assert stats.failed == 1
    assert records.read_lines(paths.queue) == []
    assert records.read_lines(paths.fail) == ["https://example.com/bad"]
    rows = records.read_lines(paths.archive)
    assert len(rows) == 1
    assert "clip.mp4" in rows[0]
    text = "\n".join(messages)
    assert "1/2: lane 1: OK|gallery-dl  https://example.com/good  -> clip.mp4" in text
    assert "2/2: lane 1: FAILED  https://example.com/bad" in text


def test_staged_links_land_in_queue_txt(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/a"])
    records.write_lines(paths.staged_queue, ["https://example.com/c"])
    seen: list[list[str]] = []

    class WatchingWorker(FakeWorker):
        def download(self, url, **kwargs):
            seen.append(records.read_lines(paths.queue))
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", WatchingWorker)
    runtime = Runtime(paths, settings)
    stats = run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert stats is not None
    assert seen == [
        ["https://example.com/a", "https://example.com/c"],
        ["https://example.com/c"],
    ]
    assert not paths.staged_queue.exists()


class FakeBoard:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.urls: list[tuple[int, str]] = []
        self.done: list[tuple[int, str]] = []
        self.refreshes = 0
        self.paused: tuple[bool, int] = (False, 0)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def ensure(self, slots, active=()) -> None:
        pass

    def set_paused(self, paused, finishing=0) -> None:
        self.paused = (paused, finishing)

    def set_url(self, slot, index, total, url, folder="") -> None:
        self.urls.append((slot, url))

    def set_progress(self, slot, index, total, name, pct, speed, folder="") -> None:
        pass

    def set_done(self, slot, index, total, verdict, files, folder="", target="") -> None:
        self.done.append((slot, verdict))

    def idle(self, slot) -> None:
        pass

    def refresh(self) -> None:
        self.refreshes += 1


def test_capture_counts_added_and_rejected(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", retry_failed=False
    )
    runtime = Runtime(paths, settings)
    runtime.seen = {"example.com/downloaded"}
    runtime.skip_always = {"example.com/skipped"}
    runtime.failed = {"example.com/failed"}
    queued = {"example.com/queued"}
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())

    added, rejected = _capture(
        [
            "https://example.com/new",
            "https://example.com/queued",
            "https://example.com/downloaded",
            "https://example.com/skipped",
            "https://example.com/failed",
            "not a url",
        ],
        queued,
        runtime,
        knowledge,
        settings,
    )

    assert added == ["https://example.com/new"]
    assert rejected == {
        "still in the queue": 1,
        "already downloaded": 1,
        "in skip.txt": 1,
        "failed before": 1,
        "not a URL": 1,
    }


def test_a_link_already_waiting_is_reported_as_such(tmp_path):
    """Silence and "already queued" look the same to whoever copied the link.

    The same copy can arrive twice, and a link can be pasted while it waits;
    either way the answer is "it is still in the queue", not nothing.
    """
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    runtime = Runtime(paths, settings)
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())

    added, rejected = _capture(
        ["https://example.com/waiting", "https://example.com/waiting"],
        set(),
        runtime,
        knowledge,
        settings,
    )

    assert added == ["https://example.com/waiting"]
    assert rejected == {"still in the queue": 1}


def test_a_finished_link_is_no_longer_queued(tmp_path):
    """Re-pasting a link that just downloaded must say "already downloaded".

    The pass kept every link it had ever scheduled in the set the clipboard
    check consults, so a link that had left queue.txt still answered "already
    queued" - even though it was downloaded (or failed) minutes earlier.
    """
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", retry_failed=False
    )
    runtime = Runtime(paths, settings)
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    queued = {"example.com/done", "example.com/failed"}

    _settle(
        queued, runtime, knowledge,
        "https://example.com/done", "OK|gallery-dl", ["2026-11-07 17:44:55\tx\ta.png\t1"],
    )
    _settle(queued, runtime, knowledge, "https://example.com/failed", "ERROR|x", [])
    _mark_failed(runtime, knowledge, "https://example.com/failed")

    assert queued == set()

    added, rejected = _capture(
        ["https://example.com/done", "https://example.com/failed"],
        queued,
        runtime,
        knowledge,
        settings,
    )

    assert added == []
    assert rejected == {"already downloaded": 1, "failed before": 1}


def test_a_success_without_files_stays_queueable(tmp_path):
    """No archive row means nothing was recorded, so the link is not "known"."""
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    runtime = Runtime(paths, settings)
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    queued = {"example.com/empty"}

    _settle(queued, runtime, knowledge, "https://example.com/empty", "OK|gallery-dl", [])

    assert queued == set()
    assert runtime.seen == set()


def test_a_locked_archive_keeps_its_rows(tmp_path, monkeypatch):
    """An editor holding downloads.tsv must not end the pass or lose rows.

    The merge replaces the archive atomically, and a handle without delete
    sharing (Notepad, opened from the files menu) makes that fail. The rows
    stay parked in fold-pending.tsv until the next merge can write.
    """
    from dredge import archive as archive_mod

    paths = Paths(tmp_path)
    paths.ensure()
    archive_mod.append_pending(
        paths.fold_pending,
        ["2026-11-07 17:00:00\thttps://example.com/a\ta.png\t1"],
    )
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    runtime = Runtime(paths, Settings.load(paths, dest_default=tmp_path / "dest"))
    said: list[str] = []

    real_replace = Path.replace

    def blocked(self, target):
        if Path(target) == paths.archive:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", blocked)

    assert _fold(paths, runtime, knowledge, said.append, lambda: "12:00:00") is None
    assert paths.fold_pending.read_text(encoding="utf-8").count("\n") == 1
    assert any("could not be written" in line for line in said)

    monkeypatch.setattr(Path, "replace", real_replace)
    assert _fold(paths, runtime, knowledge, said.append, lambda: "12:00:01") == 1
    assert "example.com/a" in paths.archive.read_text(encoding="utf-8")
    assert paths.fold_pending.read_text(encoding="utf-8").strip() == ""


class FakePreviewWorker:
    """A worker whose gallery-dl run found only previews on the archive."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def download(self, url, *, dest, flat, dest_tpl, ignore_records, lane_log,
                 result, archive, slot, **kwargs):
        Path(lane_log).write_text(f"[test] {url}\r\n", encoding="utf-8")
        Path(result).write_text("PREVIEW|gallery-dl", encoding="utf-8")
        return "PREVIEW|gallery-dl"


def test_a_preview_only_post_is_parked_in_skip_txt(tmp_path, monkeypatch):
    """Previews are not a failure: skip.txt, not fail.txt, and no retry loop."""
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/preview"])
    monkeypatch.setattr(orchestrator, "Worker", FakePreviewWorker)

    runtime = Runtime(paths, settings)
    stats = run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda line: None,
    )

    assert stats is not None
    assert stats.parked == 1
    assert stats.failed == 0
    assert records.read_lines(paths.skip) == ["https://example.com/preview"]
    assert records.read_lines(paths.fail) == []
    assert records.read_lines(paths.queue) == []
    assert "example.com/preview" in runtime.skip_always


class FakePrivateWorker:
    """A worker whose yt-dlp run found a private video."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def download(self, url, *, dest, flat, dest_tpl, ignore_records, lane_log,
                 result, archive, slot, **kwargs):
        Path(lane_log).write_text(f"[test] {url}\r\n", encoding="utf-8")
        Path(result).write_text("PRIVATE|yt-dlp", encoding="utf-8")
        return "PRIVATE|yt-dlp"


def test_a_private_video_is_parked_in_skip_txt(tmp_path, monkeypatch):
    """A private video cannot be fixed by retrying: skip.txt, not fail.txt."""
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://www.iwara.tv/video/abc"])
    monkeypatch.setattr(orchestrator, "Worker", FakePrivateWorker)

    runtime = Runtime(paths, settings)
    stats = run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda line: None,
    )

    assert stats is not None
    assert stats.parked == 1
    assert stats.failed == 0
    assert records.read_lines(paths.skip) == ["https://www.iwara.tv/video/abc"]
    assert records.read_lines(paths.fail) == []


class FakePendingWorker:
    """A worker whose gallery-dl run found the post not imported yet."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def download(self, url, *, dest, flat, dest_tpl, ignore_records, lane_log,
                 result, archive, slot, **kwargs):
        Path(lane_log).write_text(f"[test] {url}\r\n", encoding="utf-8")
        Path(result).write_text("PENDING|gallery-dl", encoding="utf-8")
        return "PENDING|gallery-dl"


def test_a_pending_post_is_parked_in_skip_txt(tmp_path, monkeypatch):
    """A post the mirror has not imported yet is not a failure to retry."""
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/pending"])
    monkeypatch.setattr(orchestrator, "Worker", FakePendingWorker)

    runtime = Runtime(paths, settings)
    stats = run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda line: None,
    )

    assert stats is not None
    assert stats.parked == 1
    assert stats.failed == 0
    assert records.read_lines(paths.skip) == ["https://example.com/pending"]
    assert records.read_lines(paths.fail) == []


def test_a_terminal_gets_one_line_per_lane(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=2,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/good"])
    monkeypatch.setattr(orchestrator, "Worker", FakeWorker)
    board = FakeBoard()
    monkeypatch.setattr(orchestrator, "_make_board", lambda out: board)

    runtime = Runtime(paths, settings)
    stats = run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=orchestrator.say,
    )

    assert stats is not None
    assert board.started == 1
    assert board.stopped == 1
    # the URL line is re-rendered while the lane has no file name yet, so the
    # folder link can appear as soon as the worker publishes it
    assert board.urls[0] == (1, "https://example.com/good")
    assert {slot for slot, _url in board.urls} == {1}
    assert board.done == [(1, "OK|gallery-dl")]
    assert board.refreshes >= 1


def test_catch_up_queue_adds_a_line_written_during_a_pass(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    records.write_lines(paths.queue, [
        "https://example.com/a",
        "https://example.com/b",
    ])
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    ordered = ["https://example.com/a"]
    pending = [0]
    remaining = ["https://example.com/a"]
    queued = {"example.com/a"}

    added = _catch_up_queue(
        paths, knowledge, ordered, pending, remaining, queued, settings
    )

    assert added == ["https://example.com/b"]
    assert ordered == ["https://example.com/a", "https://example.com/b"]
    assert pending == [0, 1]
    assert remaining == ["https://example.com/a", "https://example.com/b"]
    assert "example.com/b" in queued
    assert _catch_up_queue(
        paths, knowledge, ordered, pending, remaining, queued, settings
    ) == []


def test_catch_up_queue_ignores_links_already_downloaded(tmp_path):
    """An expanded listing re-adds its known items; the record decides.

    With yt-dlp's own archive gone, downloads.tsv is the only thing that stops a
    creator page from re-queueing everything it lists, so the catch-up must
    consult it too (the pass-start plan already does).
    """
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    records.write_lines(paths.queue, [
        "https://example.com/old",
        "https://example.com/new",
    ])
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    ordered: list[str] = []
    pending: list[int] = []
    remaining: list[str] = []
    queued: set[str] = set()

    added = _catch_up_queue(
        paths, knowledge, ordered, pending, remaining, queued, settings,
        {"example.com/old"},
    )

    assert added == ["https://example.com/new"]
    assert ordered == ["https://example.com/new"]


def test_catch_up_queue_drains_staged_urls(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    records.write_lines(paths.queue, ["https://example.com/a"])
    records.write_lines(paths.staged_queue, [
        "https://example.com/c",
        "https://example.com/d",
    ])
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    ordered = ["https://example.com/a"]
    pending = [0]
    remaining = ["https://example.com/a"]
    queued = {"example.com/a"}

    added = _catch_up_queue(
        paths, knowledge, ordered, pending, remaining, queued, settings
    )

    assert added == ["https://example.com/c", "https://example.com/d"]
    assert ordered == [
        "https://example.com/a",
        "https://example.com/c",
        "https://example.com/d",
    ]
    assert remaining == ordered
    assert "example.com/c" in queued and "example.com/d" in queued
    assert not paths.staged_queue.exists()
    # the expanded links stay visible in queue.txt while they run
    assert records.read_lines(paths.queue) == [
        "https://example.com/a",
        "https://example.com/c",
        "https://example.com/d",
    ]


def test_files_summary_lists_every_name():
    rows = [
        "2026-01-01 00:00:00\turl:1\ta.mp4;b.mp4\t1;2",
        "2026-01-01 00:00:00\turl:2\tc.mp4\t3",
        "2026-01-01 00:00:00\turl:3\ta.mp4\t1",
    ]
    assert _files_summary(rows) == "  -> a.mp4; b.mp4; c.mp4"
    assert _files_summary([]) == ""


def test_a_finished_line_links_the_file_and_the_folder(tmp_path, monkeypatch):
    from dredge import paint as paint_module

    monkeypatch.setattr(paint_module, "enabled", lambda: True)
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/good"])

    class LinkedWorker(FakeWorker):
        """Publishes the lane folder and the file the worker wrote."""

        def download(self, url, **kwargs):
            slot = kwargs["slot"]
            dest = tmp_path / "dest" / "creator"
            dest.mkdir(parents=True, exist_ok=True)
            clip = dest / "clip.mp4"
            clip.write_bytes(b"x" * 10)
            Path(paths.lane_dest(slot)).write_text(str(dest), encoding="utf-8")
            Path(paths.lane_files(slot)).write_text(str(clip), encoding="utf-8")
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", LinkedWorker)
    runtime = Runtime(paths, settings)
    messages: list[str] = []
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=messages.append,
    )

    text = "\n".join(messages)
    creator = paint_module.file_uri(tmp_path / "dest" / "creator")
    clip = paint_module.file_uri(tmp_path / "dest" / "creator" / "clip.mp4")
    assert paint_module.link("lane 1", creator) in text
    assert paint_module.link("  -> clip.mp4", clip) in text


def test_a_running_lane_links_the_folder_the_file_goes_to(tmp_path):
    from dredge import paint as paint_module

    paths = Paths(tmp_path)
    paths.ensure()
    root = tmp_path / "dest"
    creator = root / "creator"
    paths.lane_dest(1).write_text(str(root), encoding="utf-8")
    paths.lane_target(1).write_text(str(creator / "clip.mp4"), encoding="utf-8")

    assert orchestrator._lane_folder_uri(paths, 1) == paint_module.file_uri(creator)


def test_the_last_published_target_wins(tmp_path):
    from dredge import paint as paint_module

    paths = Paths(tmp_path)
    paths.ensure()
    first = tmp_path / "dest" / "creator" / "a.jpg"
    last = tmp_path / "dest" / "creator" / "b.jpg"
    paths.lane_target(1).write_text(
        "\n".join(str(p) for p in (first, last)) + "\n", encoding="utf-8"
    )

    assert orchestrator._lane_folder_uri(paths, 1) == paint_module.file_uri(last.parent)


def test_without_a_target_the_lane_links_its_destination(tmp_path):
    from dredge import paint as paint_module

    paths = Paths(tmp_path)
    paths.ensure()
    root = tmp_path / "dest"
    paths.lane_dest(1).write_text(str(root), encoding="utf-8")

    assert orchestrator._lane_folder_uri(paths, 1) == paint_module.file_uri(root)


def test_a_queued_name_goes_into_that_subfolder(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, ["https://example.com/good\tSome Author"])
    seen: list[tuple[str, Path, str]] = []

    class RecordingWorker(FakeWorker):
        def download(self, url, **kwargs):
            seen.append((url, Path(kwargs["dest"]), kwargs["dest_tpl"]))
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", RecordingWorker)
    runtime = Runtime(paths, settings)
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    author = tmp_path / "dest" / "Some Author"
    assert seen == [
        ("https://example.com/good", author, str(author / settings.tpl_flat))
    ]


def test_a_queued_path_is_used_exactly(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    elsewhere = tmp_path / "elsewhere"
    records.write_lines(paths.queue, [f"https://example.com/good\t{elsewhere}"])
    seen: list[tuple[Path, str]] = []

    class RecordingWorker(FakeWorker):
        def download(self, url, **kwargs):
            seen.append((Path(kwargs["dest"]), kwargs["dest_tpl"]))
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", RecordingWorker)
    runtime = Runtime(paths, settings)
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert seen == [(elsewhere, str(elsewhere / settings.tpl_flat))]


def test_a_failed_line_keeps_its_destination(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    line = "https://example.com/bad\tSome Author"
    records.write_lines(paths.queue, [line])
    monkeypatch.setattr(orchestrator, "Worker", FakeWorker)

    runtime = Runtime(paths, settings)
    run_pass(
        runtime, Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert records.read_lines(paths.fail) == [line]


def test_a_relative_destination_lands_where_the_absolute_one_would(
    tmp_path, monkeypatch,
):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    root = tmp_path / "dest"
    records.write_lines(paths.queue, [
        "https://example.com/good\tbluethebone\\38549292"
    ])
    seen: list[Path] = []

    class RecordingWorker(FakeWorker):
        def download(self, url, **kwargs):
            seen.append(Path(kwargs["dest"]))
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", RecordingWorker)
    run_pass(
        Runtime(paths, settings), Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert seen == [root / "bluethebone" / "38549292"]


def test_clipboard_inbox_is_drained_into_the_queue(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    records.write_lines(paths.clip_inbox, [
        "https://example.com/a",
        "https://example.com/b\tSome Author",
    ])

    added, _already = orchestrator.drain_clipboard(paths, settings)

    assert added == [
        "https://example.com/a",
        "https://example.com/b\tSome Author",
    ]
    assert records.read_lines(paths.queue) == added
    assert not paths.clip_inbox.exists()


def test_clipboard_drain_keeps_the_pause_and_loses_no_duplicates(tmp_path):
    """A copy while paused may queue the link twice and must not resume.

    Windows can update the clipboard more than once for one copy (different
    text flavours hash differently), so the same URL can reach the inbox twice.
    Draining must keep one line and leave the paused flag alone.
    """
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    paths.paused.touch()
    records.write_lines(paths.clip_inbox, [
        "https://example.com/a",
        "https://example.com/a",
        "https://example.com/b",
    ])

    added, _already = orchestrator.drain_clipboard(paths, settings)

    assert added == ["https://example.com/a", "https://example.com/b"]
    assert records.read_lines(paths.queue) == added
    assert paths.paused.exists()


def test_clipboard_drain_is_skipped_when_capture_is_off(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", no_capture=True
    )
    records.write_lines(paths.clip_inbox, ["https://example.com/a"])

    assert orchestrator.drain_clipboard(paths, settings) == ([], 0)
    assert records.read_lines(paths.queue) == []
    assert paths.clip_inbox.exists()


def test_the_clipboard_waits_in_the_inbox_while_paused(tmp_path, monkeypatch):
    """A pause must not touch queue.txt: a copied URL waits in the inbox.

    The point of the pause is that the four record files can be opened and
    edited; draining the clipboard into queue.txt would defeat it.
    """
    from dredge import cli
    from dredge.engines import Engines

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    runtime = Runtime(paths, settings)
    knowledge = Knowledge({}, SiteRule(host="*"), MirrorTable())
    paths.paused.touch()
    records.write_lines(paths.clip_inbox, ["https://example.com/a"])

    monkeypatch.setattr(cli, "_start_capture", lambda runtime: None)

    class Stop(Exception):
        pass

    def unpause(*args, **kwargs):
        # the paused branch polls; once unpaused the loop tries a pass again
        paths.paused.unlink(missing_ok=True)

    def resume(*args, **kwargs):
        # still paused while the branch ran, so nothing has been queued yet
        assert records.read_lines(paths.queue) == []
        raise Stop

    monkeypatch.setattr(cli.menu, "wait", unpause)
    monkeypatch.setattr(cli, "run_pass", resume)

    with pytest.raises(Stop):
        cli.run_loop(runtime, knowledge, Engines())

    assert records.read_lines(paths.queue) == []
    # resuming is what moves it: the inbox is drained by the next pass
    assert orchestrator.drain_clipboard(paths, settings)[0] == ["https://example.com/a"]
    assert records.read_lines(paths.queue) == ["https://example.com/a"]


def test_a_pause_mid_pass_stops_new_lanes(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, [
        "https://example.com/one",
        "https://example.com/two",
    ])

    class PausingWorker(FakeWorker):
        def download(self, url, **kwargs):
            paths.paused.touch()
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", PausingWorker)
    stats = run_pass(
        Runtime(paths, settings), Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert stats is not None
    assert stats.ok_total == 1
    assert records.read_lines(paths.queue) == ["https://example.com/two"]


def test_a_completion_after_the_pause_writes_nothing(tmp_path, monkeypatch):
    """Once paused, the record files belong to the user.

    A lane that was already running can still finish; its verdict must not
    rewrite queue.txt or append to fail.txt, or an edit made while paused would
    be lost. The link stays in queue.txt and is judged again on resume.
    """
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=2,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, [
        "https://example.com/slow",
        "https://example.com/bad",
    ])

    class SlowPausingWorker(FakeWorker):
        def download(self, url, **kwargs):
            if "slow" in url:
                paths.paused.touch()
                time.sleep(0.1)
            else:
                time.sleep(1.2)      # finishes after the pass has frozen the files
            return super().download(url, **kwargs)

    monkeypatch.setattr(orchestrator, "Worker", SlowPausingWorker)
    stats = run_pass(
        Runtime(paths, settings), Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=lambda message: None,
    )

    assert stats is not None
    assert stats.failed == 1
    assert records.read_lines(paths.fail) == []
    assert "https://example.com/bad" in records.read_lines(paths.queue)


def test_completion_line_colours_the_timestamp_and_url(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", max_downloads=1,
        no_capture=True, progress_every_seconds=0,
    )
    records.write_lines(paths.queue, [
        "https://example.com/good",
        "https://example.com/bad",
    ])
    monkeypatch.setattr(orchestrator, "Worker", FakeWorker)
    monkeypatch.setattr(orchestrator.paint, "enabled", lambda: True)

    messages: list[str] = []
    run_pass(
        Runtime(paths, settings), Knowledge({}, SiteRule(host="*"), MirrorTable()),
        orchestrator.Engines(), out=messages.append,
    )

    text = "\n".join(messages)
    assert "\x1b[94m[" in text
    assert "\x1b[92mhttps://example.com/good\x1b[0m" in text
    assert "\x1b[91mhttps://example.com/bad\x1b[0m" in text

