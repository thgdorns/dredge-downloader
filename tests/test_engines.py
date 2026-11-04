import sys

from dredge.engines import lane_status, run


def test_ytdlp_progress_marker_gives_percent_and_speed(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "[download] Destination: D:\\Downloads\\clip.mp4\n"
        "dredge-yt 52428800 104857600 5242880\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "clip.mp4"
    assert pct == "50.0%"
    assert speed == "5.0 MiB/s"


def test_the_last_progress_marker_wins(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "dredge-yt 100 200 1024\n"
        "dredge-yt 150 200 2048\n",
        encoding="utf-8",
    )
    _, pct, speed = lane_status(log)
    assert pct == "75.0%"
    assert speed == "2.0 KiB/s"


def test_gallery_dl_progress_marker_and_bare_path(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "D:\\Downloads\\creator\\image.jpg\n"
        "dredge-gdl 1048576 2097152 2097152 50\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "image.jpg"
    assert pct == "50%"
    assert speed == "2.0 MiB/s"


def test_missing_log_and_no_progress(tmp_path):
    assert lane_status(tmp_path / "nope.log") == ("", "", "")
    log = tmp_path / "lane1.log"
    log.write_text("[download] Destination: D:\\Downloads\\clip.mp4\n", encoding="utf-8")
    file, pct, speed = lane_status(log)
    assert file == "clip.mp4"
    assert (pct, speed) == ("", "")


def test_gallery_dl_size_suffixes_are_understood(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text("dredge-gdl 4.78M 1.59M 12.58M 38\n", encoding="utf-8")
    _, pct, speed = lane_status(log)
    assert pct == "38%"
    assert speed == "1.6 MiB/s"


def test_run_stops_an_engine_that_would_retry_forever(tmp_path):
    code = (
        "import time\n"
        "print('Server returned 509 (over quota)', flush=True)\n"
        "time.sleep(30)\n"
    )
    rc, text = run(
        [sys.executable, "-c", code], cwd=tmp_path,
        stop_on=r"509|over quota", timeout=15,
    )
    assert rc != 0
    assert "engine stopped" in text
