import time

from dredge import clipboard
from dredge.clipboard import extract_urls


def test_extract_urls_dedupes_and_trims_punctuation():
    text = "look: https://example.com/a). and https://example.com/b?x=1, twice https://example.com/a"
    assert extract_urls(text) == ["https://example.com/a", "https://example.com/b?x=1"]


def test_ignores_non_urls():
    assert extract_urls("no links here") == []


def test_a_tab_after_the_url_keeps_the_destination():
    assert extract_urls("https://example.com/a\tSome Author") == [
        "https://example.com/a\tSome Author"
    ]
    assert extract_urls("https://example.com/a\tG:\\Downloads\\Author") == [
        "https://example.com/a\tG:\\Downloads\\Author"
    ]


def test_text_after_the_url_without_a_tab_is_ignored():
    assert extract_urls("https://example.com/a and some words") == [
        "https://example.com/a"
    ]


def test_capture_appends_polled_urls_to_the_inbox(tmp_path, monkeypatch):
    class FakeWatcher:
        def __init__(self, interval: float = 0.25) -> None:
            self.sent = False

        def poll(self):
            if self.sent:
                return []
            self.sent = True
            return ["https://example.com/a"]

    monkeypatch.setattr(clipboard, "ClipboardWatcher", FakeWatcher)
    inbox = tmp_path / "clip-inbox.txt"
    capture = clipboard.Capture(inbox, interval=0.01)
    capture.start()
    try:
        for _ in range(100):
            if inbox.exists() and inbox.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.01)
    finally:
        capture.stop()

    assert inbox.read_text(encoding="utf-8").splitlines() == ["https://example.com/a"]
