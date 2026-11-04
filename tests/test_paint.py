from dredge import menu, orchestrator, paint
from dredge.config import Paths


def test_paint_wraps_and_strips(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)

    text = paint.red("boom")

    assert text == "\x1b[31mboom\x1b[0m"
    assert paint.strip(text) == "boom"


def test_paint_is_plain_when_colour_is_off(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: False)

    assert paint.yellow("careful") == "careful"
    assert paint.paint("x", "no-such-colour") == "x"
    coloured = "\x1b[36mchecks\x1b[0m"
    assert paint.strip(coloured) == "checks"


def test_menu_paints_the_title_items_and_hint(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)
    items = [menu.MenuItem("1", "one"), menu.MenuItem("2", "two", color="yellow")]

    text = menu.render("checks", items, close="back")

    assert "\x1b[36mchecks\x1b[0m" in text
    assert "   1) one" in text
    assert "\x1b[33m   2) two\x1b[0m" in text
    assert "\x1b[90m   Esc) back\x1b[0m" in text


def test_say_keeps_the_mirror_log_plain(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)
    paths = Paths(tmp_path)
    paths.ensure()

    orchestrator.say(paint.green("[12:00:00] Finished."), paths)

    assert "\x1b[32m" in capsys.readouterr().out
    log = (paths.logs / "dl8.log").read_text(encoding="utf-8")
    assert "\x1b[" not in log
    assert "[12:00:00] Finished." in log
