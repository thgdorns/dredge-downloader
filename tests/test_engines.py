import sys

from dredge.engines import lane_status, lane_status_tail, run


def test_gdown_progress_bar_gives_percent_and_speed(tmp_path):
    """Drive lanes showed only the URL: gdown reports through a tqdm bar."""
    log = tmp_path / "lane1.log"
    log.write_text(
        "Downloading...\n"
        "To: C:\\Downloads\\clip.mp4\n"
        "  0%|          | 0.00/1.22G [00:00<?, ?B/s]\n"
        "  1%|          | 7.86M/1.22G [00:00<01:24, 14.3MB/s]\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "clip.mp4"
    assert pct == "1%"
    assert speed == "13.6 MiB/s"


def test_gdown_without_a_rate_keeps_the_last_known_speed(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "To: C:\\Downloads\\clip.mp4\n"
        " 50%|#####     | 610M/1.22G [00:20<00:20, 30.5MB/s]\n"
        " 50%|#####     | 611M/1.22G [00:20<?, ?B/s]\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "clip.mp4"
    assert pct == "50%"
    assert speed == "29.1 MiB/s"


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


def test_megatools_progress_gives_filename_percent_and_speed(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "Re 3P2 eroero HD1080p.mp4: 27.03% - 256.0 MiB (268435456 bytes) "
        "of 947.2 MiB (969.8 KiB/s)\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "Re 3P2 eroero HD1080p.mp4"
    assert pct == "27.0%"
    assert speed == "969.8 KiB/s"


def test_a_gallery_dl_url_is_shown_whole(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "[gallery-dl] https://kemono.cr/patreon/user/15280992\n",
        encoding="utf-8",
    )
    file, pct, speed = lane_status(log)
    assert file == "https://kemono.cr/patreon/user/15280992"
    assert (pct, speed) == ("", "")


def test_lane_status_tail_reads_the_ends_of_a_big_log(tmp_path):
    log = tmp_path / "lane1.log"
    log.write_text(
        "[download] Destination: D:\\Downloads\\clip.mp4\n"
        + "noise line\n" * 2000
        + "dredge-yt 52428800 104857600 5242880\n",
        encoding="utf-8",
    )
    assert lane_status_tail(log) == lane_status(log)


def test_run_gives_child_processes_a_utf8_stdio(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    rc, out = run(
        [
            sys.executable, "-c",
            "import os; print(os.environ.get('PYTHONIOENCODING'))",
        ],
        cwd=tmp_path,
    )

    assert rc == 0
    assert out.strip() == "utf-8"
