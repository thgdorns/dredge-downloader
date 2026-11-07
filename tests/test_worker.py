import json
import os
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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
        if dest and "--write-info-json" in args and spec.get("info") is not None:
            # the post metadata gallery-dl writes even when every file is skipped
            info_dir = dest / spec.get("info_dir", "")
            info_dir.mkdir(parents=True, exist_ok=True)
            (info_dir / "info.json").write_text(
                json.dumps(spec["info"]), encoding="utf-8"
            )
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
        strict: bool | None = None, **setting_overrides):
    paths = Paths(tmp_path)
    settings = Settings.load(
        paths, dest_default=tmp_path / "dest", dry_run=False, **setting_overrides
    )
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


def test_a_preview_only_post_is_parked_without_fetching_the_text(
    tmp_path, monkeypatch
):
    """The default is the park, not the description: it is a preference."""
    behaviour = {"gdl": {
        "rc": 0,
        "text": "[pawchive][info] 1: Skipping abc.jpg ('preview only')",
        "files": [],
        "info": {"id": "1", "title": "A post", "content": "<p>Hello</p>"},
        "info_dir": "creator/1",
    }}

    verdict, root = run(
        tmp_path, monkeypatch, behaviour, url="https://example.com/post/1"
    )

    assert verdict == "PREVIEW|gallery-dl"
    assert not list((root / "dest").rglob("*.description"))
    assert not list((root / "dest").rglob("info.json"))


def test_a_preview_only_post_keeps_its_text_when_asked(tmp_path, monkeypatch):
    """With preview_description on, the post text is saved beside nothing.

    gallery-dl skips a preview by design, so this used to look like a plain
    failure. It is not one: no engine will ever get the original out of the
    archive, and the post text is the only thing that can still be saved.
    """
    behaviour = {"gdl": {
        "rc": 0,
        "text": "[pawchive][info] 1: Skipping abc.jpg ('preview only')",
        "files": [],
        "info": {
            "id": "1",
            "title": "A post",
            "content": "<p>Hello &amp; welcome</p><p>second line</p>",
            "published": "2025-12-30T22:02:47",
        },
        "info_dir": "creator/1",
    }}

    verdict, root = run(
        tmp_path, monkeypatch, behaviour, url="https://example.com/post/1",
        preview_description="on",
    )

    assert verdict == "PREVIEW|gallery-dl"
    description = root / "dest" / "creator" / "1" / "1_A post.description"
    assert description.exists()
    text = description.read_text(encoding="utf-8")
    assert "Hello & welcome" in text
    assert "second line" in text
    assert "https://example.com/post/1" in text


def test_a_missing_gallery_dl_token_asks_the_pass_to_authorize(tmp_path, monkeypatch):
    """A pinned gallery-dl site without a token hands the login to the pass.

    gallery-dl is the only engine that can fetch pixiv, so a token-less run
    must not look like a dead link: the worker reports AUTH and the pass runs
    ``gallery-dl oauth:<site>`` once, then retries.
    """
    verdict, _ = run(
        tmp_path, monkeypatch,
        {"gdl": {"rc": 1, "text": "[pixiv][error] AuthenticationError: "
                                 "'refresh-token' required."}},
        rule=parse_rule("pixiv.net engine=gdl oauth=pixiv"),
        url="https://www.pixiv.net/artworks/1",
    )

    assert verdict == "AUTH|pixiv"


def test_a_missing_token_without_an_oauth_rule_is_a_plain_failure(
    tmp_path, monkeypatch
):
    verdict, _ = run(
        tmp_path, monkeypatch,
        {"gdl": {"rc": 1, "text": "[pixiv][error] AuthenticationError: "
                                 "'refresh-token' required."}},
        rule=parse_rule("pixiv.net engine=gdl"),
        url="https://www.pixiv.net/artworks/1",
    )

    assert verdict == "FAILED"


def test_a_blocked_mega_ip_fails_fast_and_says_so(tmp_path, monkeypatch):
    """EBLOCKED makes megatools sleep for minutes; the lane must stop instead."""
    import re

    rule = parse_rule('mega.nz engine=megadl')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    stops: list[str | None] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        stops.append(stop_on)
        return 1, (
            "ERROR: Download failed for 'https://mega.nz/file/x#y': API call "
            "'g' failed: Server returned error EBLOCKED"
        )

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(paths, settings, Engines(megadl='megatools'), knowledge(rule))
    verdict = worker.download(
        'https://mega.nz/file/x#y',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'QUOTA|mega.nz'
    assert stops == [worker_module._QUOTA_STOP]
    assert re.search(worker_module._QUOTA_STOP, 'Server returned error EBLOCKED')
    log = (tmp_path / 'lane1.log').read_text(encoding='utf-8')
    assert 'blocked this IP' in log


def test_a_drive_folder_is_named_after_the_folder_not_its_id(tmp_path, monkeypatch):
    rule = parse_rule('drive.google.com engine=gdown')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    paths.cookies.parent.mkdir(parents=True, exist_ok=True)
    paths.cookies.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index('-O') + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / 'clip.mp4').write_bytes(b'x' * 10)
        return 0, 'Download completed'

    monkeypatch.setattr(worker_module, 'run', fake)
    monkeypatch.setattr(
        worker_module, 'drive_folder_name', lambda fid, timeout=15.0: '加长HMV'
    )
    worker = Worker(paths, settings, Engines(gdown='gdown'), knowledge(rule))
    verdict = worker.download(
        'https://drive.google.com/drive/folders/FOLDERID?usp=sharing',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|gdown'
    assert Path(calls[0][calls[0].index('-O') + 1]).name == '加长HMV'


def test_a_drive_folder_falls_back_to_the_id(monkeypatch):
    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'not a drive page'

    monkeypatch.setattr(worker_module.urllib.request, 'urlopen', lambda *a, **k: Fake())
    assert worker_module.drive_folder_name('abc123') == ''


def test_a_drive_folder_name_is_cleaned(monkeypatch):
    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'<title>a/b: c &#x27;d&#x27; - Google Drive</title>'

    monkeypatch.setattr(worker_module.urllib.request, 'urlopen', lambda *a, **k: Fake())
    assert worker_module.drive_folder_name('abc123') == "a_b_ c 'd'"


def test_a_ppixiv_page_in_the_fragment_becomes_a_range(tmp_path, monkeypatch):
    """#ppixiv?page=5 means the fifth picture, and --range is 1-based too."""
    from dredge.worker import page_from_fragment

    assert page_from_fragment("https://www.pixiv.net/artworks/1#ppixiv?page=5") == 5
    assert page_from_fragment("https://www.pixiv.net/artworks/1#ppixiv?page=0") == 0
    assert page_from_fragment("https://www.pixiv.net/artworks/1#ppixiv") is None
    assert page_from_fragment("https://www.pixiv.net/artworks/1") is None

    rule = parse_rule('pixiv.net engine=gdl')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        dest = Path(args[args.index('-d') + 1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / 'page5.png').write_bytes(b'x' * 200_000)
        return 0, ''

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(paths, settings, Engines(gdl='gallery-dl'), knowledge(rule))
    verdict = worker.download(
        'https://www.pixiv.net/artworks/151761889#ppixiv?page=5',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|gallery-dl'
    argv = calls[0]
    assert '--range' in argv
    assert argv[argv.index('--range') + 1] == '5'


def test_a_link_without_a_page_uses_the_rule_default(tmp_path, monkeypatch):
    """pixiv's rule says page=1: a bare artwork link means the first picture."""
    rule = parse_rule('pixiv.net engine=gdl page=1')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        dest = Path(args[args.index('-d') + 1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / 'p0.png').write_bytes(b'x' * 200_000)
        return 0, ''

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(paths, settings, Engines(gdl='gallery-dl'), knowledge(rule))
    verdict = worker.download(
        'https://www.pixiv.net/artworks/151761889',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|gallery-dl'
    argv = calls[0]
    assert argv[argv.index('--range') + 1] == '1'


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


def test_a_file_already_on_disk_is_a_success(tmp_path, monkeypatch):
    """Without the download archive, yt-dlp skips by noticing the file.

    That line must count as a download, not as a tier that produced nothing:
    otherwise every re-queued URL whose file exists would land in fail.txt.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "already.mp4"
    target.write_bytes(b"x" * 200_000)
    text = f"[download] {target} has already been downloaded"

    verdict, _ = run(tmp_path, monkeypatch, {"yt": {"rc": 0, "text": text}})

    assert verdict == "OK|yt-dlp"


def test_a_private_video_is_parked_not_failed(tmp_path, monkeypatch):
    """yt-dlp names the condition, and no retry can change it: skip.txt, not fail.txt."""
    text = "ERROR: [iwara] abc: Private video. Login if you have permissions to watch."
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": text},
        "yt": {"rc": 1, "text": text},
    })
    assert verdict == "PRIVATE|yt-dlp"


def test_a_maintenance_page_is_failed_not_offline(tmp_path, monkeypatch):
    text = "[gallery-dl][error] HttpError: 503 Service Unavailable (down for maintenance)"
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 1, "text": text},
        "yt": {"rc": 1, "text": text},
    })
    assert verdict == "FAILED"


def test_a_pending_file_import_is_failed_not_offline(tmp_path, monkeypatch):
    """The post is alive; the mirror has not imported its file yet."""
    text = (
        "[pawchive][warning] 121114557: Incomplete/Missing file import ('pending')\n"
        "[downloader.http][warning] '404 Not Found' for "
        "'https://file.pawchive.pw/data/f1/x.jpg'\n"
        "[download][error] Failed to download 121114557_Image.jpg"
    )
    verdict, _ = run(tmp_path, monkeypatch, {
        "gdl": {"rc": 4, "text": text},
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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


def test_a_public_drive_folder_never_touches_the_cookie_jar(tmp_path, monkeypatch):
    """Drive is for public folders, so the anonymous run is the only one.

    A folder not shared with the logged-in account comes back empty when the
    request carries that account's cookies, so the jar must not be used first.
    """
    rule = parse_rule('drive.google.com engine=gdown')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    paths.cookies.parent.mkdir(parents=True, exist_ok=True)
    paths.cookies.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index('-O') + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / 'clip.mp4').write_bytes(b'x' * 200_000)
        return 0, 'Download completed'

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(paths, settings, Engines(gdown='gdown'), knowledge(rule))
    verdict = worker.download(
        'https://drive.google.com/drive/folders/FOLDERID?usp=sharing',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|gdown'
    assert len(calls) == 1
    assert '--no-cookies' in calls[0]


def test_a_private_drive_link_falls_back_to_the_cookie_jar(tmp_path, monkeypatch):
    rule = parse_rule('drive.google.com engine=gdown')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    paths.cookies.parent.mkdir(parents=True, exist_ok=True)
    paths.cookies.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        if '--no-cookies' not in args:
            target = Path(args[args.index('-O') + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / 'clip.mp4').write_bytes(b'x' * 200_000)
            return 0, 'Download completed'
        return 1, "Cannot retrieve the public link of the file."

    monkeypatch.setattr(worker_module, 'run', fake)
    worker = Worker(paths, settings, Engines(gdown='gdown'), knowledge(rule))
    verdict = worker.download(
        'https://drive.google.com/drive/folders/PRIVATEID',
        dest=tmp_path / 'dest',
        lane_log=tmp_path / 'lane1.log',
        result=tmp_path / 'lane1.result',
        archive=tmp_path / 'lane1.archive',
    )

    assert verdict == 'OK|gdown'
    assert len(calls) == 2
    assert '--no-cookies' in calls[0]
    assert '--cookies' in calls[1]


def test_filehost_engine_downloads_into_the_destination(tmp_path, monkeypatch):
    rule = parse_rule('mega.nz engine=megadl')
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / 'dest', dry_run=False)
    targets: list[Path] = []
    stops: list[str | None] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    assert verdict == 'QUOTA|mega.nz'
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
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


def test_a_filepath_template_beats_the_site_template(tmp_path, monkeypatch):
    """A queued destination is handed over as a full -o template."""
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
    wanted = str(tmp_path / "dest" / "Author" / settings.tpl_flat)
    verdict = worker.download(
        "https://example.com/post/1",
        dest=tmp_path / "dest" / "Author",
        flat=True,
        dest_tpl=wanted,
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|yt-dlp"
    yt = next(call for call in calls if "gallery-dl" not in Path(str(call[0])).stem)
    assert yt[yt.index("-o") + 1] == wanted


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


def test_the_gdown_engine_downloads_into_the_destination(tmp_path, monkeypatch):
    rule = parse_rule("drive.google.com engine=gdown")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index("-O") + 1].rstrip("\\/"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(gdown="gdown"), knowledge(rule))
    verdict = worker.download(
        "https://drive.google.com/file/d/17fkE2_NsaPmF10FZfbUxXdWzrPiLeMAv/view",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|gdown"
    argv = calls[0]
    assert argv[0] == "gdown"
    assert argv[1:3] == ["-c", "--retries"]
    assert argv[argv.index("-O") + 1].endswith(os.sep)
    assert (tmp_path / "dest" / "clip.mp4").is_file()


def test_a_drive_folder_link_gets_a_folder_named_by_its_id(tmp_path, monkeypatch):
    rule = parse_rule("drive.google.com engine=gdown")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index("-O") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    # No network in a unit test: an empty name keeps the id-based fallback.
    monkeypatch.setattr(
        worker_module, "drive_folder_name", lambda fid, timeout=15.0: ""
    )
    worker = Worker(paths, settings, Engines(gdown="gdown"), knowledge(rule))
    verdict = worker.download(
        "https://drive.google.com/drive/folders/15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|gdown"
    assert calls[0][calls[0].index("-O") + 1] == str(
        tmp_path / "dest" / "15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl"
    )
    assert (tmp_path / "dest" / "15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl" / "clip.mp4").is_file()


def test_a_short_link_is_resolved_before_the_engine_lookup(tmp_path, monkeypatch):
    rule = parse_rule("drive.google.com engine=gdown")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []
    final = "https://drive.google.com/drive/folders/15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl"

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index("-O") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    monkeypatch.setattr(
        worker_module, "resolve_short_url", lambda url, hosts: final
    )
    monkeypatch.setattr(
        worker_module, "drive_folder_name", lambda fid, timeout=15.0: ""
    )
    worker = Worker(
        paths, settings, Engines(gdown="gdown"), knowledge(rule),
        shorteners=("goo.gl",),
    )
    verdict = worker.download(
        "https://goo.gl/jkGHi0",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert verdict == "OK|gdown"
    assert calls[0][-1] == final
    assert calls[0][calls[0].index("-O") + 1] == str(
        tmp_path / "dest" / "15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl"
    )
    assert (tmp_path / "dest" / "15uNXeRBIhVvZJIhL4yTw4IsStMhUaaxl" / "clip.mp4").is_file()


def test_gdown_uses_a_throwaway_copy_of_the_cookies(tmp_path, monkeypatch):
    rule = parse_rule("drive.google.com engine=gdown")
    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    paths.cookies.parent.mkdir(parents=True, exist_ok=True)
    paths.cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    calls: list[list[str]] = []
    contents: list[str] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        if "--no-cookies" in args:
            return 1, "Cannot retrieve the public link of the file."
        cookie = Path(args[args.index("--cookies") + 1])
        contents.append(cookie.read_text(encoding="utf-8"))
        target = Path(args[args.index("-O") + 1].rstrip("\\/"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(gdown="gdown"), knowledge(rule))
    worker.download(
        "https://drive.google.com/file/d/abc/view",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    argv = calls[1]
    assert "--no-cookies" in calls[0]
    cookie_file = Path(argv[argv.index("--cookies") + 1])
    assert cookie_file != paths.cookies
    assert contents[0].startswith("# Netscape")
    assert not cookie_file.exists()   # the worker removed its copy


def test_gdown_without_a_cookie_file_asks_for_none(tmp_path, monkeypatch):
    rule = parse_rule("drive.google.com engine=gdown")
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, sink=None, timeout=None, stop_on=None, stall=None):
        calls.append(list(args))
        target = Path(args[args.index("-O") + 1].rstrip("\\/"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "clip.mp4").write_bytes(b"x" * 200_000)
        return 0, ""

    monkeypatch.setattr(worker_module, "run", fake)
    worker = Worker(paths, settings, Engines(gdown="gdown"), knowledge(rule))
    worker.download(
        "https://drive.google.com/file/d/abc/view",
        dest=tmp_path / "dest",
        lane_log=tmp_path / "lane1.log",
        result=tmp_path / "lane1.result",
        archive=tmp_path / "lane1.archive",
    )

    assert "--no-cookies" in calls[0]


