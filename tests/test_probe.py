import json

from dredge.config import Paths
from dredge.engines import Engines
from dredge.probe import Item, classify, fill_template, format_result, probe
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge

RULE = parse_rule(
    "example.com id=[0-9]{6,} list=^/profile/[^/]+(?:/videos)?$ "
    "playlist=user postid=^(\\d+)_ "
    "itemurl=https://{host}/{service}/post/{id} "
    "posturl=https://{host}/{service}/user/{user}/post/{id}"
)


def knowledge() -> Knowledge:
    return Knowledge(rules={"example.com": RULE}, global_rule=SiteRule(host="*"),
                     mirrors=MirrorTable())


def engines(yt="yt-dlp", gdl="gallery-dl") -> Engines:
    return Engines(yt=yt, gdl=gdl)


def test_fill_template_returns_empty_when_a_key_is_missing():
    assert fill_template("https://{host}/post/{id}", {"host": "e", "id": "1"}) == (
        "https://e/post/1"
    )
    assert fill_template("https://{host}/post/{id}", {"host": "e"}) == ""


def test_flat_listing_is_used_for_a_list_page(tmp_path):
    def runner(args, cwd):
        assert "--flat-playlist" in args
        return 0, "https://example.com/post/123\tOne\nhttps://example.com/post/456\tTwo\n"

    result = probe(
        "https://example.com/profile/abc/videos",
        files=False,
        knowledge=knowledge(),
        paths=Paths(tmp_path),
        engines=engines(),
        runner=runner,
    )

    assert result.source == "yt-dlp flat"
    assert [item.url for item in result.items] == [
        "https://example.com/post/123",
        "https://example.com/post/456",
    ]
    assert result.counts["new"] == 2


def test_classify_marks_every_reason():
    items = [
        Item("https://example.com/a"),
        Item("https://example.com/b"),
        Item("https://example.com/c"),
        Item("https://example.com/d"),
        Item("https://example.com/e"),
        Item("https://example.com/a"),
    ]
    out, counts = classify(
        items,
        knowledge=knowledge(),
        seen={"example.com/d"},
        queued={"example.com/a"},
        skip={"example.com/b"},
        failed={"example.com/c"},
    )

    assert [item.why for item in out] == [
        "queued", "skip.txt", "failed/offline", "downloaded", ""
    ]
    assert counts == {
        "new": 1, "queued": 1, "skip": 1, "failed": 1,
        "downloaded": 1, "unusable": 0, "duplicate": 1,
    }


def test_records_check_off_ignores_skip_and_archive():
    out, counts = classify(
        [Item("https://example.com/a")],
        knowledge=knowledge(),
        seen={"example.com/a"},
        queued=set(),
        skip={"example.com/a"},
        failed={"example.com/a"},
        archive_check=False,
    )
    assert counts["new"] == 1
    assert out[0].why == ""


def test_file_mode_uses_gallery_dl_g(tmp_path):
    def runner(args, cwd):
        assert "-g" in args
        return 0, "https://file.example/a.jpg\n| https://mirror.example/a.jpg\n"

    result = probe(
        "https://example.com/post/1",
        files=True,
        knowledge=knowledge(),
        paths=Paths(tmp_path),
        engines=engines(yt=None),
        runner=runner,
    )

    assert result.source == "gallery-dl -g"
    # "| " lines are alternative mirrors of the same file, not extra items
    assert [item.url for item in result.items] == ["https://file.example/a.jpg"]


def test_dump_json_builds_item_urls_from_the_template(tmp_path):
    payload = json.dumps([
        [2, {"service": "fanbox", "user": "someone", "id": "555", "title": "Post"}],
    ])

    def runner(args, cwd):
        assert "--dump-json" in args
        return 0, payload

    result = probe(
        "https://example.com/fanbox/user/someone",
        files=False,
        knowledge=knowledge(),
        paths=Paths(tmp_path),
        engines=engines(yt=None),
        runner=runner,
    )

    assert result.source == "gallery-dl"
    assert result.items[0].url == "https://example.com/fanbox/post/555"
    assert result.items[0].title == "Post"


def test_a_single_post_without_a_template_keeps_its_own_url(tmp_path):
    payload = json.dumps([[2, {"service": "fanbox", "user": "x", "id": "9"}]])

    result = probe(
        "https://other.example/post/9",
        files=False,
        knowledge=knowledge(),
        paths=Paths(tmp_path),
        engines=engines(yt=None),
        runner=lambda args, cwd: (0, payload),
    )

    assert result.items[0].url == "https://other.example/post/9"


def test_a_file_host_is_reported_not_listed(tmp_path):
    knowledge_ = Knowledge(
        rules={"mega.nz": parse_rule("mega.nz engine=megadl")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )
    result = probe(
        "https://mega.nz/folder/abc123",
        files=False,
        knowledge=knowledge_,
        paths=Paths(tmp_path),
        engines=engines(),
        runner=lambda args, cwd: (1, "should not run"),
    )
    assert result.counts == {"filehost": 1}
    assert result.items == []


def test_format_result_marks_each_item():
    from dredge.probe import Result

    result = Result(
        url="https://example.com/u",
        source="yt-dlp flat",
        items=[Item("https://example.com/a", "A"), Item("https://example.com/b", "", "queued")],
        counts={"new": 1, "queued": 1},
    )
    text = format_result(result)
    assert "1 new -> queue.txt" in text
    assert "https://example.com/a  [new]  A" in text
    assert "https://example.com/b  [queued]" in text


def test_probe_mode_drains_the_inbox_and_queues_the_new_items(tmp_path, monkeypatch):
    from dredge import probe as probe_mod
    from dredge import records
    from dredge.config import Runtime, Settings

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings.load(paths, dest_default=tmp_path / "dest")
    runtime = Runtime(paths, settings)

    monkeypatch.setattr(
        probe_mod.state, "read_clip_inbox", lambda p: ["https://example.com/a"]
    )
    monkeypatch.setattr(
        probe_mod, "probe",
        lambda *a, **k: probe_mod.Result(
            url="https://example.com/a", source="test",
            items=[probe_mod.Item("https://example.com/a")], counts={"new": 1},
        ),
    )

    rc = probe_mod.run_mode(
        runtime, knowledge(), engines(), files=False, out=lambda message: None
    )

    assert rc == 0
    assert records.read_lines(paths.queue) == ["https://example.com/a"]
    assert not paths.paused.exists()
