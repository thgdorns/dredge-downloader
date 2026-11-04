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

    def fake(args, *, cwd=None, sink=None, timeout=None):
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
                if "_path" in template or "filepath" in template:
                    manifest = Path(args[index + 2])
                    break
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

    def fake(args, *, cwd=None, sink=None, timeout=None):
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
