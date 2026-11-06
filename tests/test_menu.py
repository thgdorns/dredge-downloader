from dredge import menu


def test_wait_dispatches_the_pause_key(monkeypatch):
    keys = iter(["p", None])
    monkeypatch.setattr(menu, "menu_key", lambda: next(keys, None))
    calls: list[str] = []

    menu.wait(0, on_pause=lambda: calls.append("pause"))

    assert calls == ["pause"]


def test_wait_keeps_the_menu_key_working(monkeypatch):
    keys = iter(["m", None])
    monkeypatch.setattr(menu, "menu_key", lambda: next(keys, None))
    calls: list[str] = []

    menu.wait(0, on_menu=lambda: calls.append("menu"))

    assert calls == ["menu"]


def test_wait_ignores_a_pause_without_a_callback(monkeypatch):
    keys = iter(["p", None])
    monkeypatch.setattr(menu, "menu_key", lambda: next(keys, None))
    calls: list[str] = []

    menu.wait(0, on_menu=lambda: calls.append("menu"))

    assert calls == []
