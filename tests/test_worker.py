from pathlib import Path

import dredge.worker as worker_module
from dredge.config import Paths, Settings
from dredge.engines import Engines
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge
from dredge.worker import Worker


def knowledge(rule: SiteRule | None = None) -> Knowledge:
    rules = {rule.host: rule} if rule else {}
    return Knowledge(rules=rules, global_rule=SiteRule(host="*"), mirrors=MirrorTable())


def make_runner(behaviour: dict[str, dict]):
    """Fake engine call: writes the "downloaded" files and the manifest."""

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        engine = "gdl" if Path(str(args[0])).stem.lower() in ("gdl", "gallery-dl") else "yt"
        spec = behaviour.get(engine, {})
        dest = None
        for flag in ("-d", "-D", "-P"):
            if flag in args:
                dest = Path(args[args.index(flag) + 1])
                break
        manifest = None
        for index, arg in enumerate(args):
            if arg in ("--Print-to-file", "--print-to-file") and index + 2 < len(args):
                template = str(args[index + 1])
                # after_move:filepath / after:{_path} is the "what was written"
                # manifest; before_dl/prepare-after is the lane's target path and
                # after_move:extractor belongs to the extractor file
                if template.startswith(("after_move:filepath", "after:{_path}")):
                    manifest = Path(args[index + 2])
        names = spec.get("files") or ([spec["file"]] if spec.get("file") else [])
        saved: list[str] = []
        if dest:
            dest.mkdir(parents=True, exist_ok=True)
            for name in names:
                path = dest / name
                path.write_bytes(b"x" * spec.get("size", 200_000))
                saved.append(str(path))
        if manifest and saved:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("\n".join(saved), encoding="utf-8")
        extractor_file = None
        for index, arg in enumerate(args):
            if (arg in ("--Print-to-file", "--print-to-file") and index + 2 < len(args)
                    and str(args[index + 1]).endswith("extractor")):
                extractor_file = Path(args[index + 2])
        if extractor_file and spec.get("extractor"):
            extractor_file.parent.mkdir(parents=True, exist_ok=True)
            extractor_file.write_text(spec["extractor"], encoding="utf-8")
        text = spec.get("text", "")
        if sink and text:
            for line in text.splitlines():
                sink(line)
        return spec.get("rc", 1), text

    return fake


def run(tmp_path, monkeypatch, behaviour, rule=None, url="https://example.com/post/1",
        strict: bool | None = None):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    monkeypatch.setattr(worker_module, "run", make_runner(behaviour))
    worker = Worker(paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"),
                    knowledge(rule), strict_generic=strict)
    verdict = worker.download(
        url,
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )
    return verdict, tmp_path


def test_gallery_dl_success_writes_a_row(tmp_path, monkeypatch):
    verdict, root = run(tmp_path, monkeypatch, {"gdl": {"rc": 0, "file": "clip.mp4"}})

    assert verdict == "OK|gallery-dl"
    row = (root / "lane1.archive").read_text(encoding="utf-8").strip()
    fields = row.split("\t")
    assert fields[1] == "example.com/post/1"
    assert fields[2] == "clip.mp4"
    assert int(fields[3]) == 200_000
    assert (root / "lane1.result").read_text(encoding="utf-8") == "OK|gallery-dl"


def test_yt_dlp_takes_over_when_gallery_dl_fails(tmp_path, monkeypatch):
    verdict, root = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "[gallery-dl][error] Unsupported URL"},
        "yt": {"rc": 0, "file": "video.mp4"},
    })

    assert verdict == "OK|yt-dlp"
    assert "video.mp4" in (root / "lane1.archive").read_text(encoding="utf-8")


def test_every_tier_failing_is_failed(tmp_path, monkeypatch):
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "boom"},
        "yt": {"rc": 1, "text": "boom"},
    })
    assert verdict == "FAILED"


def test_a_dead_post_is_offline_not_failed(tmp_path, monkeypatch):
    dead = "ERROR: This video is no longer available"
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": dead},
        "yt": {"rc": 1, "text": dead},
    })
    assert verdict == "OFFLINE"


def test_a_private_video_is_failed_not_offline(tmp_path, monkeypatch):
    text = "ERROR: [iwara] abc: Private video. Login if you have permissions to watch."
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": text},
        "yt": {"rc": 1, "text": text},
    })
    assert verdict == "FAILED"


def test_a_maintenance_page_is_failed_not_offline(tmp_path, monkeypatch):
    text = "[gallery-dl][error] HttpError: 503 Service Unavailable (down for maintenance)"
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": text},
        "yt": {"rc": 1, "text": text},
    })
    assert verdict == "FAILED"


def test_a_cloudflare_challenge_is_failed_not_offline(tmp_path, monkeypatch):
    text = "[gallery-dl][error] 403 Forbidden: Just a moment..."
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": text},
        "yt": {"rc": 1, "text": text},
    })
    assert verdict == "FAILED"


def test_a_site_min_rule_drops_the_file_and_its_row(tmp_path, monkeypatch):
    rule = parse_rule("example.com min=1M")
    verdict, root = run(
        tmp_path, monkeypatch,
        {"gdl": {"rc": 0, "file": "preview.mp4", "size": 200_000}},
        rule=rule,
    )

    assert verdict == "OK|gallery-dl"
    assert not (root / "dest" / "preview.mp4").exists()
    assert not (root / "lane1.archive").exists()


def test_a_dry_run_never_writes_a_row(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", dry_run=True, no_update=True
    )
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"),
                    knowledge())
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict.startswith("OK|")
    assert not (tmp_path / "lane1.archive").exists()
    assert any("--simulate" in call for call in calls)


def test_a_page_asset_is_not_a_download(tmp_path, monkeypatch):
    verdict, root = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "[gallery-dl][error] Unsupported URL"},
        "yt": {"rc": 0, "files": ["adv_vert.js"]},
    })

    assert verdict == "FAILED"
    assert not (root / "lane1.archive").exists()
    # the file is left where the engine wrote it; it is simply not a download
    assert (root / "dest" / "adv_vert.js").exists()


def test_a_small_video_is_a_download(tmp_path, monkeypatch):
    verdict, root = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 0, "file": "short.mp4", "size": 50_000},
    })

    assert verdict == "OK|gallery-dl"
    row = (root / "lane1.archive").read_text(encoding="utf-8")
    assert "short.mp4" in row and "50000" in row


def test_strict_mode_blocks_a_generic_result_from_an_unknown_host(tmp_path, monkeypatch):
    verdict, root = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "Unsupported URL"},
        "yt": {"rc": 0, "file": "clip.mp4", "extractor": "generic"},
    }, strict=True)

    assert verdict == "FAILED"
    log = (root / "lane1.log").read_text(encoding="utf-8")
    assert "[strict]" in log and "generic-sites.txt" in log


def test_strict_mode_allows_a_listed_host(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "generic-sites.txt").write_text("example.com\n", encoding="utf-8")
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "Unsupported URL"},
        "yt": {"rc": 0, "file": "clip.mp4", "extractor": "generic"},
    }, strict=True)

    assert verdict == "OK|yt-dlp"


def test_a_creator_page_is_one_row_per_post(tmp_path, monkeypatch):
    rule = parse_rule(
        "example.com playlist=user postid=^(\\d+)_ "
        "posturl=https://{host}/{service}/user/{user}/post/{id}"
    )
    verdict, root = run(
        tmp_path, monkeypatch,
        {"gdl": {"rc": 0, "files": ["111_A.jpg", "111_B.jpg", "222_C.jpg"]}},
        rule=rule,
        url="https://example.com/patreon/user/someone",
    )

    rows = [row for row in (root / "lane1.archive").read_text(encoding="utf-8").splitlines()
            if row]
    assert verdict == "OK|gallery-dl"
    assert len(rows) == 2
    first, second = (row.split("\t") for row in rows)
    assert first[1].endswith("/post/111")
    assert first[2] == "111_A.jpg;111_B.jpg"
    assert second[1].endswith("/post/222")
    assert second[2] == "222_C.jpg"
    assert b"\r\r\n" not in (root / "lane1.archive").read_bytes()


def test_keep_rule_keeps_only_the_first_file(tmp_path, monkeypatch):
    rule = parse_rule("example.com keep=1")
    verdict, root = run(
        tmp_path, monkeypatch,
        {"gdl": {"rc": 0, "file": "first.jpg"}},
        rule=rule,
    )

    assert verdict == "OK|gallery-dl"
    row = (root / "lane1.archive").read_text(encoding="utf-8").strip()
    assert row.split("\t")[2] == "first.jpg"


def test_filehost_engine_downloads_into_the_destination(tmp_path, monkeypatch):
    rule = parse_rule('mega.nz engine=megadl')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    targets: list[Path] = []
    stops: list[str | None] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        stops.append(stop_on)
        target = Path(args[args.index('--path') + 1])
        targets.append(target)
        (target / 'clip.mp4').write_bytes(b'x' * 200_000)
        return 0, ''

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(
        paths, settings, Engines(megadl='megatools'), knowledge(rule)
    )
    verdict = worker.download(
        'https://mega.nz/file/abc#key',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|megadl'
    assert targets == [tmp_path / 'dest']
    assert stops == [worker_module._QUOTA_STOP]
    assert (tmp_path / 'dest' / 'clip.mp4').exists()
    assert not (paths.state / 'stage').exists()
    assert not (tmp_path / 'dest' / '~fh-lane1').exists()


def test_a_killed_mega_download_keeps_its_partial_file(tmp_path, monkeypatch):
    rule = parse_rule('mega.nz engine=megadl')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        target = Path(args[args.index('--path') + 1])
        (target / '.megatmp.ABC123').write_bytes(b'x' * 4096)
        return 1, 'WARNING: chunk download failed (Server returned 509 (over quota))'

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(
        paths, settings, Engines(megadl='megatools'), knowledge(rule)
    )
    verdict = worker.download(
        'https://mega.nz/file/abc#key',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'FAILED'
    assert (tmp_path / 'dest' / '.megatmp.ABC123').exists()
    log = (tmp_path / 'lane1.log').read_text(encoding='utf-8')
    assert 'partial data kept' in log


def test_gallery_dl_skip_lines_with_files_on_disk_are_a_success(tmp_path, monkeypatch):
    dest = tmp_path / 'dest'
    dest.mkdir()
    clip = dest / 'clip.mp4'
    clip.write_bytes(b'x' * 200_000)

    verdict, root = run(tmp_path, monkeypatch, {
        'gdl': {'rc': 0, 'text': f'# {clip}'},
    })

    assert verdict == 'OK|gallery-dl'
    row = (root / 'lane1.archive').read_text(encoding='utf-8')
    assert 'clip.mp4' in row


def test_gallery_dl_archived_file_missing_here_is_recorded_not_fetched(
    tmp_path, monkeypatch
):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        text = "# G:\\Downloads\\old\\clip.mp4"
        if sink:
            sink(text)
        return 0, text

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(gdl="gallery-dl"), knowledge())
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|gallery-dl"
    assert not (tmp_path / "dest" / "clip.mp4").exists()
    assert not any("--download-archive" in call for call in calls)
    row = (tmp_path / "lane1.archive").read_text(encoding="utf-8").strip()
    assert "example.com/post/1" in row


def test_a_mega_folder_uses_its_online_name(tmp_path, monkeypatch):
    rule = parse_rule("mega.nz engine=megadl")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    targets: list[Path] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        target = Path(args[args.index("--path") + 1])
        targets.append(target)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    monkeypatch.setattr(worker_module, "mega_folder_name", lambda url: "VR")
    worker = Worker(
        paths, settings, Engines(megadl="megatools"), knowledge(rule),
        filehosts=("mega.nz",),
    )
    verdict = worker.download(
        "https://mega.nz/folder/ABC#key",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|megadl"
    assert targets == [tmp_path / "dest" / "VR"]
    assert (tmp_path / "dest" / "VR" / "clip.mp4").exists()


def test_gallery_dl_uses_the_ytdlp_display_name_for_the_author_folder(
    tmp_path, monkeypatch
):
    rule = parse_rule("iwara.tv engine=gdl author=yt-dlp")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        if "--print" in args:
            return 0, "マレーバク大佐\n"
        dest = tmp_path / "dest"
        target = dest / "マレーバク大佐"
        target.mkdir(parents=True, exist_ok=True)
        clip = target / "clip.mp4"
        clip.write_bytes(b"x" * 200_000)
        for index, arg in enumerate(args):
            if arg == "--Print-to-file" and index + 2 < len(args):
                manifest = Path(args[index + 2])
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(str(clip), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge(rule)
    )
    verdict = worker.download(
        "https://www.iwara.tv/video/abc",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|gallery-dl"
    assert any(
        "directory=マレーバク大佐" in arg for call in calls for arg in call
    )
    assert (tmp_path / "dest" / "マレーバク大佐" / "clip.mp4").is_file()


def test_engine_yt_dlp_tries_yt_dlp_before_gallery_dl(tmp_path, monkeypatch):
    rule = parse_rule("example.com engine=yt-dlp yt=--write-description")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        if "gallery-dl" in Path(str(args[0])).stem:
            return 0, ""
        dest = Path(args[args.index("-P") + 1])
        clip = dest / "clip.mp4"
        dest.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"x" * 200_000)
        for index, arg in enumerate(args):
            if arg == "--print-to-file" and index + 2 < len(args):
                manifest = Path(args[index + 2])
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(str(clip), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge(rule)
    )
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|yt-dlp"
    assert not any("gallery-dl" in Path(str(call[0])).stem for call in calls)
    assert any(
        "--write-description" in call
        for call in calls
        if "gallery-dl" not in Path(str(call[0])).stem
    )


def test_refresh_uses_gallery_dl_archive_without_no_skip(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []
    runner = make_runner({"gdl": {"rc": 0, "file": "clip.jpg"}})

    def fake(args, **kwargs):
        calls.append(list(args))
        return runner(args, **kwargs)

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge()
    )
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        refresh=True,
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    gdl = next(
        call for call in calls
        if "gallery-dl" in Path(str(call[0])).stem
    )
    assert verdict == "OK|gallery-dl"
    assert "--no-skip" not in gdl
    assert "--download-archive" not in gdl


def test_refresh_forces_yt_dlp_to_overwrite(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []
    runner = make_runner({
        "gdl": {"rc": 1, "text": "ERROR: Unsupported URL"},
        "yt": {"rc": 0, "file": "clip.mp4"},
    })

    def fake(args, **kwargs):
        calls.append(list(args))
        return runner(args, **kwargs)

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge()
    )
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        refresh=True,
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    yt_calls = [
        call for call in calls
        if "gallery-dl" not in Path(str(call[0])).stem
    ]
    assert verdict == "OK|yt-dlp"
    assert any("--force-overwrites" in call for call in yt_calls)


def test_engine_yt_dlp_never_falls_through(tmp_path, monkeypatch):
    """engine= names one engine: a failure is FAILED, not a walk down the chain.

    Without the pin the same failure retries as yt-dlp-native, then the
    all-extractor pass, then gallery-dl - three attempts that can only repeat
    the answer, which is what made every iwara file look like it "used" four
    engines.
    """
    rule = parse_rule("example.com engine=yt-dlp")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        return 1, "ERROR: aria2c exited with code 1"

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge(rule)
    )
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "FAILED"
    assert len(calls) == 1
    assert not any("gallery-dl" in Path(str(call[0])).stem for call in calls)


def test_engine_gdl_never_falls_through(tmp_path, monkeypatch):
    rule = parse_rule("example.com engine=gdl")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        if "gallery-dl" in Path(str(args[0])).stem:
            return 1, "ERROR: Unsupported URL"
        dest = Path(args[args.index("-P") + 1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"), knowledge(rule)
    )
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "FAILED"
    assert len(calls) == 1
    assert "gallery-dl" in Path(str(calls[0][0])).stem


def test_engine_megadl_never_falls_through(tmp_path, monkeypatch):
    rule = parse_rule("mega.nz engine=megadl")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None):
        calls.append(list(args))
        if "megatools" in str(args[0]):
            return 1, "ERROR: 509 bandwidth limit exceeded"
        dest = Path(args[args.index("-P") + 1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(
        paths,
        settings,
        Engines(yt="yt-dlp", gdl="gallery-dl", megadl="megatools"),
        knowledge(rule),
    )
    verdict = worker.download(
        "https://mega.nz/file/abc#key",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "FAILED"
    assert len(calls) == 1
    assert "megatools" in str(calls[0][0])


def test_unpinned_host_still_walks_the_chain(tmp_path, monkeypatch):
    """No engine= in sites.txt means the cascade is still the default."""
    verdict, _root = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": "ERROR: Unsupported URL"},
        "yt": {"rc": 0, "file": "clip.mp4"},
    })

    assert verdict == "OK|yt-dlp"


def test_an_exact_destination_ignores_the_site_template(tmp_path, monkeypatch):
    """A queued destination means "files go here", not "here/<uploader>/"."""
    rule = parse_rule("example.com tpl=%(uploader)s/%(title)s.%(ext)s")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []
    runner = make_runner({"gdl": {"rc": 1, "text": "ERROR: Unsupported URL"},
                          "yt": {"rc": 0, "file": "clip.mp4"}})

    def fake(args, **kwargs):
        calls.append(list(args))
        return runner(args, **kwargs)

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"),
                    knowledge(rule))
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest" / "Author",
        flat=True,
        exact=True,
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|yt-dlp"
    yt = next(call for call in calls if "gallery-dl" not in Path(str(call[0])).stem)
    assert yt[yt.index("-o") + 1] == settings.tpl_flat
    assert (tmp_path / "dest" / "Author" / "clip.mp4").is_file()


def test_an_expanded_listing_keeps_the_queued_destination(tmp_path, monkeypatch):
    rule = parse_rule("example.com list=^/user/")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)

    def fake(args, **kwargs):
        return 0, "https://example.com/post/1\nhttps://example.com/post/2\n"

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"),
                    knowledge(rule))
    verdict = worker.download(
        "https://example.com/user/x",
        dest=tmp_path / "dest",
        queue_spec="Some Author",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|expanded"
    staged = paths.staged_queue.read_text(encoding="utf-8").splitlines()
    assert staged == [
        "https://example.com/post/1\tSome Author",
        "https://example.com/post/2\tSome Author",
    ]

