from pathlib import Path

from dredge.config import Paths, Settings, save_setting, write_gallery_runtime


def test_top_level_settings_after_destination_sections_are_read(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(
        """# header
[dest:default]
path = D:/Downloads
layout = creator

[dest:porn]
path = D:/OTR/porn
layout = flat

max_downloads = 7
cookies_browser = test
""",
        encoding="utf-8",
    )

    settings = Settings.load(paths)

    assert settings.max_downloads == 7
    assert settings.cookies_browser == "test"


def test_save_setting_replaces_only_that_line(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(
        """# header
[dest:default]
path = D:/Downloads

max_downloads = 4
idle_seconds = 3
""",
        encoding="utf-8",
    )

    save_setting(paths.settings, "max_downloads", "7")

    text = paths.settings.read_text(encoding="utf-8")
    assert "max_downloads = 7" in text
    assert text.count("max_downloads") == 1
    assert "# header" in text and "idle_seconds = 3" in text
    assert Settings.load(paths).max_downloads == 7


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

