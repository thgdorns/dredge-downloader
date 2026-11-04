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
