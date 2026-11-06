import io

from rich.console import Console

from dredge.lanes import LaneBoard


def test_board_renders_a_live_frame():
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=100, highlight=False)
    board = LaneBoard(console=console)
    board.start()
    try:
        board.set_url(1, 2, 10, "https://example.com/post/1")
        board.set_progress(1, 2, 10, "clip.mp4", "45.3%", "1.6 MiB/s")
        board.refresh()
        assert "clip.mp4" in stream.getvalue()
    finally:
        board.stop()


def test_board_line_moves_from_url_to_progress_to_verdict():
    board = LaneBoard()

    board.set_url(1, 2, 10, "https://example.com/post/1")
    assert "https://example.com/post/1" in board._lines[1].plain

    board.set_progress(1, 2, 10, "clip.mp4", "45.3%", "1.6 MiB/s")
    line = board._lines[1].plain
    assert "clip.mp4" in line
    assert "45.3%" in line
    assert "1.6 MiB/s" in line

    board.set_done(1, 2, 10, "OK|gallery-dl", " -> clip.mp4")
    assert "OK|gallery-dl" in board._lines[1].plain


def test_a_finished_line_lingers_before_idle():
    board = LaneBoard(linger=60)
    board.set_done(1, 1, 1, "OK|gallery-dl", "")

    board.idle(1)

    assert "OK|gallery-dl" in board._lines[1].plain


def test_board_keeps_active_lanes_when_the_limit_shrinks():
    board = LaneBoard()
    board.ensure([1, 2, 3])
    board.ensure([1, 2], active=[3])

    assert sorted(board._lines) == [1, 2, 3]

    board.ensure([1], active=[])
    assert sorted(board._lines) == [1]


def test_the_lane_label_and_file_name_link_to_the_folder():
    board = LaneBoard()
    board.set_progress(
        1, 2, 10, "clip.mp4", "45.3%", "1.6 MiB/s", folder="file:///G:/x/"
    )

    spans = board._lines[1].spans
    assert "link file:///G:/x/" in str(spans[0].style)     # "lane 1: "
    assert "link file:///G:/x/" in str(spans[2].style)     # the file name


def test_a_url_in_the_lane_is_not_hijacked():
    board = LaneBoard()
    board.set_progress(
        1, 2, 10, "https://example.com/x", "", "", folder="file:///G:/x/"
    )

    line = board._lines[1]
    start = line.plain.index("https://example.com/x")
    assert not [span for span in line.spans if span.start <= start < span.end]


def test_a_finished_lane_links_the_file_and_the_folder():
    board = LaneBoard()
    board.set_done(
        1, 2, 10, "OK|gallery-dl", "  -> clip.mp4",
        folder="file:///G:/x/", target="file:///G:/x/clip.mp4",
    )

    spans = board._lines[1].spans
    assert "link file:///G:/x/" in str(spans[0].style)
    assert "link file:///G:/x/clip.mp4" in str(spans[-1].style)


def test_without_uris_the_line_stays_plain():
    board = LaneBoard()
    board.set_done(1, 2, 10, "OK|gallery-dl", "  -> clip.mp4")

    assert all("link" not in str(span.style) for span in board._lines[1].spans)
