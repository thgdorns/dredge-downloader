from dredge import state


def test_wait_returns_as_soon_as_the_inbox_appears(tmp_path):
    queue = tmp_path / "queue.txt"
    queue.write_text("", encoding="utf-8")
    inbox = tmp_path / "clip-inbox.txt"
    ticks: list[int] = []

    def sleep(_seconds: float) -> None:
        ticks.append(1)
        if len(ticks) == 2:
            inbox.write_text("https://example.com/a\n", encoding="utf-8")

    state.wait_for_change(queue, inbox, 5, sleep=sleep)

    assert len(ticks) == 2


def test_wait_returns_when_the_queue_is_touched(tmp_path):
    import os

    queue = tmp_path / "queue.txt"
    queue.write_text("a\n", encoding="utf-8")
    inbox = tmp_path / "clip-inbox.txt"
    ticks: list[int] = []

    def sleep(_seconds: float) -> None:
        ticks.append(1)
        if len(ticks) == 2:
            os.utime(queue, (1_700_000_000, 1_700_000_000))

    state.wait_for_change(queue, inbox, 5, sleep=sleep)

    assert len(ticks) == 2
