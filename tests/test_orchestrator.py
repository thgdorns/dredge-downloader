import threading
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
    _free_slot,
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

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def ensure(self, slots, active=()) -> None:
        pass

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

    added = orchestrator.drain_clipboard(paths, settings)

    assert added == [
        "https://example.com/a",
        "https://example.com/b\tSome Author",
    ]
    assert records.read_lines(paths.queue) == added
    assert not paths.clip_inbox.exists()


def test_clipboard_drain_is_skipped_when_capture_is_off(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", no_capture=True
    )
    records.write_lines(paths.clip_inbox, ["https://example.com/a"])

    assert orchestrator.drain_clipboard(paths, settings) == []
    assert records.read_lines(paths.queue) == []
    assert paths.clip_inbox.exists()


def test_run_loop_drains_the_clipboard_while_paused(tmp_path, monkeypatch):
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
    monkeypatch.setattr(cli.menu, "wait", lambda *a, **k: None)

    real_drain = cli.drain_clipboard

    def drain_then_unpause(paths_, settings_):
        added = real_drain(paths_, settings_)
        paths_.paused.unlink(missing_ok=True)
        return added

    monkeypatch.setattr(cli, "drain_clipboard", drain_then_unpause)

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(cli, "run_pass", stop)

    with pytest.raises(Stop):
        cli.run_loop(runtime, knowledge, Engines())

    assert records.read_lines(paths.queue) == ["https://example.com/a"]

