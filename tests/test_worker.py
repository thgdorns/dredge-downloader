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
    """Fake engine call: writes the "downloaded" file and the manifest."""

    def fake(args, *, cwd=None, sink=None, timeout=None):
        engine = "gdl" if Path(str(args[0])).stem.lower() in ("gdl", "gallery-dl") else "yt"
        spec = behaviour.get(engine, {})
        dest = None
        for flag in ("-d", "-D", "-P"):
            if flag in args:
                dest = Path(args[args.index(flag) + 1])
                break
        manifest = None
        for flag in ("--Print-to-file", "--print-to-file"):
            if flag in args:
                manifest = Path(args[args.index(flag) + 2])
        if spec.get("file") and dest:
            dest.mkdir(parents=True, exist_ok=True)
            saved = dest / spec["file"]
            saved.write_bytes(b"x" * spec.get("size", 200_000))
            if manifest:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(str(saved), encoding="utf-8")
        text = spec.get("text", "")
        if sink and text:
            for line in text.splitlines():
                sink(line)
        return spec.get("rc", 1), text

    return fake


def run(tmp_path, monkeypatch, behaviour, rule=None, url="https://example.com/post/1"):
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dest_default=tmp_path / "dest", dry_run=False)
    monkeypatch.setattr(worker_module, "run", make_runner(behaviour))
    worker = Worker(paths, settings, Engines(yt="yt-dlp", gdl="gallery-dl"),
                    knowledge(rule))
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
