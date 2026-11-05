"""Duplicate-check matrix for the kemono/pawchive mirror pair.

The tests use synthetic rules that mirror the live site lines, so they do not
need network access or the user's config. They document which URL shapes share
an archive key and which are deliberately separate.
"""

import json

from dredge import probe, records
from dredge.archive import load_sets
from dredge.config import Paths, Settings
from dredge.engines import Engines
from dredge.plan import build_plan
from dredge.probe import Item
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge
from dredge.worker import Worker

PAW = "https://pawchive.pw/patreon/user/100622150/post/147340591"
KEM = "https://kemono.cr/patreon/user/100622150/post/147340591"
SU = "https://kemono.su/patreon/user/100622150/post/147340591"
PARTY = "https://kemono.party/patreon/user/100622150/post/147340591"
NEW = "https://pawchive.pw/patreon/user/100622150/post/999999"
PAW_PROFILE = "https://pawchive.pw/patreon/user/100622150"
KEM_PROFILE = "https://kemono.cr/patreon/user/100622150"
FILE1 = "https://file.pawchive.pw/data/8e/46/8e460dff6396baa0f2cc73d1c36602052ced3f1a5f8c8f48cc996701142fbec2.png"
FILE2 = "https://file.pawchive.pw/data/3e/f3/3ef3f9bc270a439afff90f7a119bd155c5ef21ab5628de5daf9f7f0e41d3a981.png"


def knowledge() -> Knowledge:
    rules = {
        "pawchive.pw": parse_rule(
            "pawchive.pw keep=all min=0 serial playlist=user "
            "postid=^(\\d+)_ "
            "posturl=https://{host}/{service}/user/{user}/post/{id}"
        ),
        "kemono.cr": parse_rule(
            "kemono.cr keep=all min=0 also=kemono.su,kemono.party serial "
            "playlist=user postid=^(\\d+)_ "
            "posturl=https://{host}/{service}/user/{user}/post/{id}"
        ),
    }
    return Knowledge(
        rules=rules,
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(
            mirrors={
                "kemono.cr": "pawchive.pw",
                "kemono.su": "pawchive.pw",
                "kemono.party": "pawchive.pw",
            }
        ),
    )


def plan(urls, seen):
    return build_plan(
        urls,
        seen=seen,
        failed=set(),
        skip_always=set(),
        knowledge=knowledge(),
        archive_check=True,
    )


def test_mirror_hosts_share_one_archive_key():
    k = knowledge()
    paw = k.canon(PAW)
    kem = k.canon(KEM)

    assert paw != kem
    assert k.dl_url(KEM) == PAW
    assert set(k.keys(PAW)) == {paw}
    assert set(k.keys(KEM)) == {kem, paw}
    assert set(k.keys(SU)) == {k.canon(SU), paw}
    assert set(k.keys(PARTY)) == {k.canon(PARTY), paw}


def test_single_post_is_skipped_after_either_mirror_is_seen():
    k = knowledge()

    for seen_url, queued in (
        (PAW, KEM),
        (KEM, PAW),
        (SU, PAW),
        (PARTY, KEM),
    ):
        result = plan([queued], set(k.keys(seen_url)))
        assert result.downloaded == 1, (seen_url, queued, result)
        assert result.urls == []


def test_twin_links_in_one_queue_collapse_to_one_line():
    result = plan([PAW, KEM, SU, PARTY], set())

    assert result.urls == [PAW]
    assert result.duplicates == 3


def test_profile_download_records_posts_not_the_profile_url(tmp_path):
    k = knowledge()
    paths = Paths(tmp_path)
    settings = Settings.load(paths, dry_run=False)
    worker = Worker(paths, settings, Engines(), k)

    for name in ("111_A.jpg", "111_B.jpg", "222_C.jpg"):
        (tmp_path / name).write_bytes(b"x")
    manifest = tmp_path / "lane.files"
    manifest.write_text(
        "\n".join(str(tmp_path / name) for name in ("111_A.jpg", "111_B.jpg", "222_C.jpg")),
        encoding="utf-8",
    )
    archive = tmp_path / "rows.tsv"
    playlist = k.playlist(PAW_PROFILE)
    assert playlist is not None

    worker._write_rows(
        archive, PAW_PROFILE, "gallery-dl", tmp_path, manifest,
        k.rule(PAW_PROFILE), playlist, [],
    )

    rows = archive.read_text(encoding="utf-8").splitlines()
    assert any("/post/111\t" in row for row in rows)
    assert any("/post/222\t" in row for row in rows)
    assert not any(PAW_PROFILE in row for row in rows)

    seen, _ = load_sets(archive, k)
    assert k.canon(PAW_PROFILE) not in seen
    assert k.canon("https://pawchive.pw/patreon/user/100622150/post/111") in seen
    assert plan([PAW_PROFILE], seen).urls == [PAW_PROFILE]


def test_probe_post_recognises_a_mirror_twin():
    k = knowledge()

    for url in (KEM, SU, PARTY):
        out, counts = probe.classify(
            [Item(url)],
            knowledge=k,
            seen={k.canon(PAW)},
            queued=set(),
            skip=set(),
            failed=set(),
            archive_check=True,
        )
        assert counts["downloaded"] == 1, (url, counts)
        assert out[0].why == "downloaded"


def test_probe_dedupes_twin_items_in_one_list():
    k = knowledge()

    _out, counts = probe.classify(
        [Item(PAW), Item(KEM), Item(SU), Item(PARTY)],
        knowledge=k,
        seen={k.canon(PAW)},
        queued=set(),
        skip=set(),
        failed=set(),
        archive_check=True,
    )

    assert counts["downloaded"] == 1, counts
    assert counts["duplicate"] == 3, counts


def test_probe_posts_keep_a_genuinely_new_post():
    k = knowledge()
    _, counts = probe.classify(
        [Item(NEW)],
        knowledge=k,
        seen={k.canon(PAW)},
        queued=set(),
        skip=set(),
        failed=set(),
        archive_check=True,
    )

    assert counts["new"] == 1


def test_probe_files_are_not_covered_by_the_post_archive():
    k = knowledge()

    _, counts = probe.classify(
        [Item(FILE1), Item(FILE2)],
        knowledge=k,
        seen={k.canon(PAW)},
        queued=set(),
        skip=set(),
        failed=set(),
        archive_check=True,
    )

    assert counts["new"] == 2, counts


def test_probe_files_skip_a_file_url_that_was_downloaded_itself():
    k = knowledge()

    _, counts = probe.classify(
        [Item(FILE1), Item(FILE2)],
        knowledge=k,
        seen={k.canon(FILE1)},
        queued=set(),
        skip=set(),
        failed=set(),
        archive_check=True,
    )

    assert counts == {
        "new": 1, "queued": 0, "skip": 0, "failed": 0,
        "downloaded": 1, "unusable": 0, "duplicate": 0,
    }


def test_probe_post_level_builds_urls_from_the_probed_host(tmp_path):
    k = knowledge()
    payload = json.dumps(
        [[2, {"service": "patreon", "user": "100622150", "id": "147340591",
              "title": "T"}]]
    )

    def runner(args, cwd):
        return 0, payload

    result = probe.probe(
        KEM_PROFILE,
        files=False,
        knowledge=k,
        paths=Paths(tmp_path),
        engines=Engines(gdl="gallery-dl", yt=None),
        seen={k.canon(PAW)},
        queued=set(),
        skip=set(),
        failed=set(),
        archive_check=True,
        runner=runner,
    )

    assert result.items[0].url == KEM
    assert result.counts["downloaded"] == 1
    assert result.counts["new"] == 0


def test_skip_and_fail_sets_are_mirror_aware():
    k = knowledge()
    skip = records.keyed_set([PAW], k)
    failed = records.keyed_set([KEM], k)

    assert any(key in skip for key in k.keys(KEM))
    assert any(key in failed for key in k.keys(PAW))

    result = build_plan(
        [KEM],
        seen=set(),
        failed=set(),
        skip_always=skip,
        knowledge=k,
        archive_check=True,
    )
    assert result.marked_skip == 1

    result = build_plan(
        [PAW],
        seen=set(),
        failed=failed,
        skip_always=set(),
        knowledge=k,
        archive_check=True,
    )
    assert result.failed_skip == 1


def test_archive_rows_add_the_mirror_key(tmp_path):
    k = knowledge()
    archive = tmp_path / "downloads.tsv"
    archive.write_text(
        "2026-01-01 00:00:00\t" + k.canon(KEM) + "\tA.jpg\t1\r\n",
        encoding="utf-8",
    )

    seen, archived = load_sets(archive, k)

    assert k.canon(KEM) in seen
    assert k.canon(PAW) in seen
    assert k.canon(PAW) in archived
