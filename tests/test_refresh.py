import json
from pathlib import Path

from dredge import refresh
from dredge.config import Paths

HASH_A = "a" * 64
HASH_B = "b" * 64
SAMPLE = json.dumps(
    [
        [2, {"id": "1", "edited": "2024-01-01T00:00:00", "category": "pawchive"}],
        [3, "https://file.test/a", {"num": 1, "hash": HASH_A}],
        [3, "https://file.test/b", {"num": 2, "hash": HASH_B}],
    ]
)


def test_parse_metadata_reads_one_post_and_its_hashes():
    meta = refresh.parse_metadata(SAMPLE)

    assert meta is not None
    assert meta.edited == "2024-01-01T00:00:00"
    assert meta.hashes == (HASH_A, HASH_B)


def test_parse_metadata_ignores_log_lines_around_the_json():
    meta = refresh.parse_metadata("[pawchive][warning] pending\n" + SAMPLE)

    assert meta is not None
    assert meta.hashes == (HASH_A, HASH_B)


def test_parse_metadata_refuses_a_creator_page():
    two_posts = json.dumps(
        [
            [2, {"id": "1", "edited": "2024-01-01"}],
            [2, {"id": "2", "edited": "2024-01-02"}],
        ]
    )

    assert refresh.parse_metadata(two_posts) is None


def test_hash_filter_keeps_only_new_content():
    expression = refresh.hash_filter((HASH_A, HASH_B))

    assert expression == f"hash not in ('{HASH_A}','{HASH_B}')"


def test_hash_filter_is_empty_without_usable_hashes():
    assert refresh.hash_filter(()) == ""
    assert refresh.hash_filter(("not a hash",)) == ""


def test_hash_filter_is_dropped_when_the_command_line_would_be_too_long():
    many = tuple(f"{index:064x}" for index in range(1000))

    assert refresh.hash_filter(many) == ""


def test_revision_ledger_round_trips(tmp_path):
    ledger = tmp_path / "revisions.tsv"

    refresh.remember(ledger, "https://example.com/post/1", "2024-01-01", (HASH_A,))
    refresh.remember(ledger, "https://example.com/post/2", "2024-01-02", (HASH_B,))

    rows = refresh.load(ledger)
    assert rows["https://example.com/post/1"].edited == "2024-01-01"
    assert rows["https://example.com/post/1"].hashes == (HASH_A,)
    assert rows["https://example.com/post/2"].hashes == (HASH_B,)
    assert rows["https://example.com/post/2"].folder == ""


def test_revision_ledger_keeps_the_post_folder(tmp_path):
    ledger = tmp_path / "revisions.tsv"

    refresh.remember(ledger, "https://example.com/post/1", "2024-01-01", (HASH_A,), r"D:\Downloads\Creator\1")

    assert refresh.load(ledger)["https://example.com/post/1"].folder == r"D:\Downloads\Creator\1"


def test_fetch_post_asks_for_quiet_json(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    paths = Paths(tmp_path)

    def fake(args, **kwargs):
        calls.append(list(args))
        return 0, SAMPLE

    monkeypatch.setattr(refresh, "run", fake)

    meta = refresh.fetch_post("gallery-dl", paths, "https://example.com/post/1")

    assert meta is not None
    assert "-q" in calls[0] and "-j" in calls[0]
    assert calls[0][-1] == "https://example.com/post/1"


def test_post_json_hashes_walks_attachments_and_archives():
    text = json.dumps(
        {
            "edited": "2024-01-01",
            "attachments": [{"hash": HASH_A}],
            "archives": [{"hash": HASH_B}],
        }
    )

    assert refresh.post_json_hashes(text) == (HASH_A, HASH_B)


def test_remember_from_manifest_uses_the_post_sidecar(tmp_path):
    post = tmp_path / "Creator" / "1"
    post.mkdir(parents=True)
    clip = post / "clip.jpg"
    clip.write_bytes(b"x")
    (post / "post.json").write_text(
        json.dumps({"edited": "2024-01-01", "attachments": [{"hash": HASH_A}]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "lane.files"
    manifest.write_text(str(clip), encoding="utf-8")
    Path(str(manifest) + ".hashes").write_text(HASH_A, encoding="utf-8")
    ledger = tmp_path / "revisions.tsv"

    assert refresh.remember_from_manifest(ledger, "https://example.com/post/1", manifest)

    row = refresh.load(ledger)["https://example.com/post/1"]
    assert row.edited == "2024-01-01"
    assert row.hashes == (HASH_A,)
    assert row.folder == str(post)


def test_remember_from_manifest_is_a_no_op_without_a_sidecar(tmp_path):
    clip = tmp_path / "clip.jpg"
    clip.write_bytes(b"x")
    manifest = tmp_path / "lane.files"
    manifest.write_text(str(clip), encoding="utf-8")

    assert not refresh.remember_from_manifest(
        tmp_path / "revisions.tsv", "https://example.com/post/1", manifest
    )
    assert not (tmp_path / "revisions.tsv").exists()


def test_remember_from_manifest_ignores_hashes_of_files_that_were_not_written(tmp_path):
    clip = tmp_path / "clip.jpg"
    clip.write_bytes(b"x")
    (tmp_path / "post.json").write_text(
        json.dumps({"edited": "2024-01-01", "attachments": [{"hash": HASH_A}]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "lane.files"
    manifest.write_text(str(clip), encoding="utf-8")

    assert not refresh.remember_from_manifest(
        tmp_path / "revisions.tsv", "https://example.com/post/1", manifest
    )
    assert not (tmp_path / "revisions.tsv").exists()
