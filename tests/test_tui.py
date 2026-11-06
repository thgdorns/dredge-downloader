import os
import sys
from pathlib import Path

from dredge import records, state, tui
from dredge.config import Paths, Runtime, Settings
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


def setup(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    knowledge = Knowledge(
        rules={"example.com": parse_rule("example.com id=[0-9]{6,}")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )
    runtime = Runtime(paths, Settings.load(paths))
    records.write_lines(paths.fail, ["https://example.com/a", "https://example.com/b"])
    records.write_lines(paths.queue, ["https://example.com/queued"])
    records.write_lines(paths.skip, ["https://example.com/skip"])
    state.update_failed_set(runtime, knowledge)
    return paths, runtime, knowledge


def test_retry_failed_moves_fail_txt_into_the_queue(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)

    tui.retry_failed(runtime, knowledge)

    assert records.read_lines(paths.queue) == [
        "https://example.com/queued",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert records.read_lines(paths.fail) == []
    assert "example.com/a" not in runtime.failed


def test_retry_failed_does_not_queue_a_line_twice(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)
    records.write_lines(paths.queue, ["https://example.com/queued", "https://example.com/a"])

    tui.retry_failed(runtime, knowledge)

    assert records.read_lines(paths.queue).count("https://example.com/a") == 1


def test_skip_failed_moves_fail_txt_into_skip_txt(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)

    tui.skip_failed(runtime, knowledge)

    assert records.read_lines(paths.skip) == [
        "https://example.com/skip",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert records.read_lines(paths.fail) == []
    assert "example.com/a" in runtime.skip_always


def test_skip_failed_does_not_add_a_link_already_skipped(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)
    records.write_lines(paths.fail, ["https://example.com/skip"])

    tui.skip_failed(runtime, knowledge)

    assert records.read_lines(paths.skip) == ["https://example.com/skip"]


def test_checks_menu_reports_the_current_switches(tmp_path, monkeypatch):
    _, runtime, knowledge = setup(tmp_path)
    shown: list[str] = []
    colors: list[str] = []

    def fake_select(title, items, close="close"):
        shown.append(title)
        shown.extend(item.label for item in items)
        colors.extend(item.color for item in items)

    monkeypatch.setattr(tui.menu, "select", fake_select)
    tui.show_checks_menu(runtime, knowledge)

    text = "\n".join(shown)
    assert "records check   [ON]" in text
    assert "retry failed" in text
    assert "retry skipped" in text
    assert "skip failed" in text
    assert "lanes" in text
    assert "pause/resume" in text
    assert "strict mode     [OFF]" in text
    # ON is Gray, OFF is Yellow: the legacy menu warned about a disabled check
    assert colors[0] == "gray"


def test_strict_mode_option_toggles(tmp_path, monkeypatch):
    _, runtime, knowledge = setup(tmp_path)
    answers = iter(["8"])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))

    tui.show_checks_menu(runtime, knowledge)

    assert runtime.strict_generic is True


def test_lane_count_option_asks_for_a_number(tmp_path, monkeypatch):
    paths, runtime, knowledge = setup(tmp_path)
    answers = iter(["6", "7"])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))
    monkeypatch.setattr(tui.menu, "prompt_line", lambda prompt: next(answers, ""))

    tui.show_checks_menu(runtime, knowledge)

    assert runtime.max_downloads == 7
    assert "max_downloads = 7" in paths.settings.read_text(encoding="utf-8")


def test_retry_skipped_moves_skip_txt_into_the_queue(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)

    tui.retry_skipped(runtime, knowledge)

    assert records.read_lines(paths.queue) == [
        "https://example.com/queued",
        "https://example.com/skip",
    ]
    assert records.read_lines(paths.skip) == []
    assert "example.com/skip" not in runtime.skip_always


def test_retry_skipped_does_not_queue_a_line_twice(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)
    records.write_lines(paths.queue, [
        "https://example.com/queued",
        "https://example.com/skip",
    ])

    tui.retry_skipped(runtime, knowledge)

    assert records.read_lines(paths.queue).count("https://example.com/skip") == 1


def test_latest_run_log_is_the_newest_file(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.logs.mkdir(parents=True, exist_ok=True)
    old = paths.logs / "run-20260101-000000.log"
    new = paths.logs / "run-20260102-000000.log"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert tui.latest_run_log(paths.logs) == new


def test_latest_run_log_is_none_without_logs(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()

    assert tui.latest_run_log(paths.logs) is None


def test_files_menu_opens_the_latest_run_log(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.logs.mkdir(parents=True, exist_ok=True)
    old = paths.logs / "run-20260101-000000.log"
    new = paths.logs / "run-20260102-000000.log"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    runtime = Runtime(paths, Settings.load(paths))
    opened: list[Path] = []
    answers = iter(["6", None])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))
    monkeypatch.setattr(tui, "open_record", opened.append)

    tui.show_files_menu(runtime)

    assert opened == [new]


def test_tools_menu_runs_the_url_scan(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.tools.mkdir(parents=True, exist_ok=True)
    script = paths.tools / "url_scan.py"
    script.write_text("# tool\n", encoding="utf-8")
    runtime = Runtime(paths, Settings.load(paths))
    calls: list[list[str]] = []
    answers = iter(["1", None])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))
    monkeypatch.setattr(
        tui.subprocess, "call", lambda argv: calls.append(argv) or 0
    )

    tui.show_tools_menu(runtime)

    assert calls == [[sys.executable, str(script), "--root", str(paths.root)]]


def test_tools_menu_writes_the_record_report(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure()
    runtime = Runtime(paths, Settings.load(paths))
    seen: list[Path] = []
    written = {"fail": paths.root / "reports" / "fail.html"}
    answers = iter(["2", None])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))
    monkeypatch.setattr(
        tui, "write_reports", lambda root: seen.append(root) or written
    )

    tui.show_tools_menu(runtime)

    assert seen == [paths.root]


def test_the_main_menu_opens_the_tools_submenu(tmp_path, monkeypatch):
    _, runtime, knowledge = setup(tmp_path)
    opened: list[Runtime] = []
    answers = iter(["3", None])
    monkeypatch.setattr(tui.menu, "select", lambda *a, **k: next(answers, None))
    monkeypatch.setattr(tui, "show_tools_menu", opened.append)

    tui.show_dredge_menu(runtime, knowledge)

    assert opened == [runtime]


def test_retry_failed_keeps_the_destination(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)
    line = "https://example.com/c\tSome Author"
    records.write_lines(paths.fail, [line])
    state.update_failed_set(runtime, knowledge)

    tui.retry_failed(runtime, knowledge)

    assert records.read_lines(paths.queue) == ["https://example.com/queued", line]


def test_skip_failed_keeps_the_destination(tmp_path):
    paths, runtime, knowledge = setup(tmp_path)
    line = "https://example.com/c\tSome Author"
    records.write_lines(paths.fail, [line])
    state.update_failed_set(runtime, knowledge)

    tui.skip_failed(runtime, knowledge)

    assert records.read_lines(paths.skip) == ["https://example.com/skip", line]
