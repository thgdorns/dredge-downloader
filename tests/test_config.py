from pathlib import Path

from dredge.config import Paths, Settings, write_gallery_runtime


def test_defaults_match_the_legacy_script(tmp_path):
    settings = Settings.load(Paths(tmp_path))
    assert settings.max_downloads == 4
    assert settings.idle_seconds == 3
    assert settings.progress_every_seconds == 15
    assert settings.min_video_bytes == 102400
    assert settings.cookies_browser == "floorp"
    assert settings.dest_default == Path("E:/Downloads")
    assert settings.dest_porn == Path("E:/OTR/porn")


def test_conf_file_overrides_defaults(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dredge.conf").write_text(
        "# machine-specific values\nmax-downloads = 7\ntpl_porn = %(id)s.%(ext)s\n"
        "dest_porn = G:\\porn\nunknown = ignored\n",
        encoding="utf-8",
    )
    settings = Settings.load(Paths(tmp_path))
    assert settings.max_downloads == 7
    assert settings.tpl_porn == "%(id)s.%(ext)s"
    assert settings.dest_porn == Path("G:/porn")


def test_command_line_overrides_the_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dredge.conf").write_text("max_downloads = 7\n", encoding="utf-8")
    settings = Settings.load(Paths(tmp_path), max_downloads=2)
    assert settings.max_downloads == 2


def test_a_relative_destination_resolves_against_the_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dredge.conf").write_text("dest_default = media\n", encoding="utf-8")
    settings = Settings.load(Paths(tmp_path))
    assert settings.dest_default == tmp_path / "media"


def test_gallery_runtime_config_is_regenerated(tmp_path):
    paths = Paths(tmp_path)
    settings = Settings.load(paths)
    write_gallery_runtime(paths, settings)
    text = paths.gallery_runtime.read_text(encoding="utf-8")
    assert '"keywords-default": ""' in text
    assert settings.gdl_dir in text


def test_paths_are_relative_to_the_root(tmp_path):
    paths = Paths(tmp_path)
    assert paths.queue == tmp_path / "data" / "queue.txt"
    assert paths.lane_log(2) == tmp_path / "logs" / "lane2.log"
    assert paths.staged_queue == tmp_path / "data" / "state" / "queue-requeue.txt"
