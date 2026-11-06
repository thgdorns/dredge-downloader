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


def test_link_wraps_and_strips(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)

    text = paint.link("clip.mp4", "file:///G:/Downloads/clip.mp4")

    assert text == (
        "\x1b]8;;file:///G:/Downloads/clip.mp4\x1b\\clip.mp4\x1b]8;;\x1b\\"
    )
    assert paint.strip(text) == "clip.mp4"


def test_link_is_plain_when_colour_is_off(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: False)

    assert paint.link("clip.mp4", "file:///G:/Downloads/clip.mp4") == "clip.mp4"
    assert paint.link("", "file:///x") == ""
    assert paint.link("x", "") == "x"


def test_file_uri_escapes_spaces_and_stays_absolute(tmp_path):
    uri = paint.file_uri(tmp_path / "a b" / "clip.mp4")

    assert uri.startswith("file:///")
    assert uri.endswith("/a%20b/clip.mp4")
    assert " " not in uri


def test_file_uri_keeps_non_ascii_literal(tmp_path):
    """The Windows shell cannot decode %XX as UTF-8 in a file: URI."""
    uri = paint.file_uri(tmp_path / "一库" / "クリップ.mp4")

    assert "一库" in uri
    assert "クリップ" in uri
    assert "%E" not in uri


def test_file_uri_needs_an_absolute_path():
    assert paint.file_uri("relative/clip.mp4") == ""


def test_the_mirror_log_drops_hyperlinks(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)
    paths = Paths(tmp_path)
    paths.ensure()

    orchestrator.say(paint.link("lane 1", "file:///G:/Downloads/creator/"), paths)

    assert "\x1b]8;;" in capsys.readouterr().out
    log = (paths.logs / "dl8.log").read_text(encoding="utf-8")
    assert "lane 1" in log
    assert "file:///" not in log


def test_blue_and_light_url_colours(monkeypatch):
    monkeypatch.setattr(paint, "enabled", lambda: True)

    assert paint.blue("[12:00:00]") == "\x1b[94m[12:00:00]\x1b[0m"
    assert paint.lightgreen("https://ok") == "\x1b[92mhttps://ok\x1b[0m"
    assert paint.lightred("https://bad") == "\x1b[91mhttps://bad\x1b[0m"
