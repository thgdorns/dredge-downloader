import threading
from pathlib import Path

from dredge import orchestrator, records
from dredge.config import Paths, Runtime, Settings
from dredge.orchestrator import (
    Lane,
    _capture,
    _catch_up_queue,
    _free_slot,
    _take_next,
    run_pass,
)
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


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
                 result, archive, slot):
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
    assert "1/2: lane 1: OK|gallery-dl -> clip.mp4  https://example.com/good" in text
    assert "2/2: lane 1: FAILED  https://example.com/bad" in text


class FakeBoard:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.urls: list[tuple[int, str]] = []
        self.done: list[tuple[int, str]] = []
        self.refreshes = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def ensure(self, slots, active=()) -> None:
        pass

    def set_url(self, slot, index, total, url) -> None:
        self.urls.append((slot, url))

    def set_progress(self, slot, index, total, name, pct, speed) -> None:
        pass

    def set_done(self, slot, index, total, verdict, files) -> None:
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
        "already queued": 1,
        "already downloaded": 1,
        "in skip.txt": 1,
        "failed before": 1,
        "not a URL": 1,
    }


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
    assert board.urls == [(1, "https://example.com/good")]
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
