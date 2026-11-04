from pathlib import Path

from dredge.config import Paths, Settings, write_gallery_runtime


def test_a_saved_lane_count_is_used_on_the_next_start(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.lanes_pref.write_text("7", encoding="utf-8")

    assert Settings.load(paths).max_downloads == 7
    assert Settings.load(paths, max_downloads=3).max_downloads == 3

    paths.lanes_pref.write_text("99", encoding="utf-8")
    assert Settings.load(paths).max_downloads == 4


def test_defaults_match_the_legacy_script(tmp_path):
    settings = Settings.load(Paths(tmp_path))
    assert settings.max_downloads == 4
    assert settings.idle_seconds == 3
    assert settings.progress_every_seconds == 15
    assert settings.cookies_browser == "floorp"
    assert settings.dest_default == Path("E:/Downloads")
    assert settings.dest_flat == Path("E:/Downloads/misc")


def test_conf_file_overrides_defaults(tmp_path):
    absolute = Path(tmp_path.anchor) / "misc"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dredge.conf").write_text(
        "# machine-specific values\nmax-downloads = 7\ntpl_flat = %(id)s.%(ext)s\n"
        f"dest_flat = {absolute}\nunknown = ignored\n",
        encoding="utf-8",
    )
    settings = Settings.load(Paths(tmp_path))
    assert settings.max_downloads == 7
    assert settings.tpl_flat == "%(id)s.%(ext)s"
    assert settings.dest_flat == absolute


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


def test_named_destinations_from_sections(tmp_path):
    videos = tmp_path / "Videos"
    flat = tmp_path / "Flat"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dredge.conf").write_text(
        f"[dest:videos]\npath = {videos}\nlayout = creator\n"
        f"[dest:flat]\npath = {flat}\nlayout = flat\ntpl = %(id)s.%(ext)s\n"
        "destination_default = videos\n",
        encoding="utf-8",
    )
    settings = Settings.load(Paths(tmp_path))

    assert set(settings.destinations) == {"videos", "flat"}
    assert settings.default_dest == "videos"
    assert settings.destinations["flat"].path == flat
    assert settings.destinations["flat"].layout == "flat"
    assert settings.destinations["flat"].tpl == "%(id)s.%(ext)s"

