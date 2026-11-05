import json

from dredge import refresh

HASH_A = "a" * 64
HASH_B = "b" * 64
KEMONO = json.dumps(
    [
        [2, {"id": "1", "edited": "2024-01-01T00:00:00"}],
        [3, "https://file.test/a", {"num": 1, "hash": HASH_A}],
        [3, "https://file.test/b", {"num": 2, "hash": HASH_B}],
    ]
)
FANTIA = json.dumps(
    [
        [2, {"post_id": "123", "post_title": "a post"}],
        [3, "https://fantia.test/a", {"num": 1, "file_id": 456}],
        [3, "https://fantia.test/b", {"num": 2, "file_id": 789}],
    ]
)


def test_parse_metadata_reads_content_hashes():
    meta = refresh.parse_metadata(KEMONO)

    assert meta is not None
    assert meta.identities == ("h:" + HASH_A, "h:" + HASH_B)
    assert meta.post_id == "1"
    assert meta.complete is True


def test_parse_metadata_reads_fantia_file_ids():
    meta = refresh.parse_metadata(FANTIA)

    assert meta is not None
    assert meta.identities == ("f:456", "f:789")
    assert meta.post_id == "123"


def test_parse_metadata_marks_a_file_without_an_identity_incomplete():
    text = json.dumps(
        [
            [2, {"id": "1", "edited": "2024-01-01"}],
            [3, "https://file.test/a", {"num": 1, "hash": ""}],
        ]
    )

    meta = refresh.parse_metadata(text)

    assert meta is not None
    assert meta.identities == ()
    assert meta.complete is False


def test_parse_metadata_ignores_log_lines_around_the_json():
    meta = refresh.parse_metadata("[pawchive][warning] pending\n" + KEMONO)

    assert meta is not None
    assert meta.identities == ("h:" + HASH_A, "h:" + HASH_B)


def test_parse_metadata_refuses_a_creator_page():
    text = json.dumps(
        [
            [2, {"id": "1", "edited": "2024-01-01"}],
            [2, {"id": "2", "edited": "2024-01-02"}],
        ]
    )

    assert refresh.parse_metadata(text) is None


def sidecar(folder, name, value):
    (folder / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")


def test_up_to_date_matches_sidecar_hashes(tmp_path):
    (tmp_path / "clip.jpg").write_bytes(b"x")
    sidecar(tmp_path, "clip.jpg", {"hash": HASH_A})
    meta = refresh.PostMeta(identities=("h:" + HASH_A,))

    assert refresh.up_to_date(meta, tmp_path)


def test_up_to_date_accepts_extra_sidecars(tmp_path):
    (tmp_path / "clip.jpg").write_bytes(b"x")
    sidecar(tmp_path, "clip.jpg", {"hash": HASH_A})
    (tmp_path / "old.jpg").write_bytes(b"old")
    sidecar(tmp_path, "old.jpg", {"hash": HASH_B})
    meta = refresh.PostMeta(identities=("h:" + HASH_A,))

    assert refresh.up_to_date(meta, tmp_path)


def test_up_to_date_rejects_a_changed_sidecar_hash(tmp_path):
    (tmp_path / "clip.jpg").write_bytes(b"x")
    sidecar(tmp_path, "clip.jpg", {"hash": HASH_A})
    meta = refresh.PostMeta(identities=("h:" + HASH_B,))

    assert not refresh.up_to_date(meta, tmp_path)


def test_up_to_date_rejects_a_folder_without_sidecars(tmp_path):
    (tmp_path / "clip.jpg").write_bytes(b"x")
    meta = refresh.PostMeta(identities=("h:" + HASH_A,))

    assert not refresh.up_to_date(meta, tmp_path)


def test_up_to_date_rejects_a_missing_folder(tmp_path):
    meta = refresh.PostMeta(identities=("h:" + HASH_A, "h:" + HASH_B))

    assert not refresh.up_to_date(meta, tmp_path / "missing")


def test_up_to_date_matches_fantia_file_ids(tmp_path):
    (tmp_path / "123_456.jpg").write_bytes(b"x")
    sidecar(tmp_path, "123_456.jpg", {"file_id": 456})
    meta = refresh.PostMeta(identities=("f:456",), post_id="123")

    assert refresh.up_to_date(meta, tmp_path)


def test_up_to_date_rejects_a_new_fantia_file_id(tmp_path):
    (tmp_path / "123_456.jpg").write_bytes(b"x")
    sidecar(tmp_path, "123_456.jpg", {"file_id": 456})
    meta = refresh.PostMeta(identities=("f:789",), post_id="123")

    assert not refresh.up_to_date(meta, tmp_path)


def test_up_to_date_is_false_for_incomplete_metadata(tmp_path):
    (tmp_path / "clip.jpg").write_bytes(b"x")
    meta = refresh.PostMeta(identities=(), complete=False)

    assert not refresh.up_to_date(meta, tmp_path)


def test_up_to_date_is_true_for_a_post_without_files(tmp_path):
    meta = refresh.PostMeta(identities=())

    assert refresh.up_to_date(meta, tmp_path)


def test_folder_ledger_round_trips(tmp_path):
    ledger = tmp_path / "folders.tsv"

    refresh.remember_folder(ledger, "https://example.com/post/1", r"D:\Downloads\A\1")
    refresh.remember_folder(ledger, "https://example.com/post/2", r"D:\Downloads\A\2")

    rows = refresh.load(ledger)
    assert rows["https://example.com/post/1"].folder == r"D:\Downloads\A\1"
    assert rows["https://example.com/post/2"].folder == r"D:\Downloads\A\2"


def test_folder_ledger_ignores_old_rows(tmp_path):
    ledger = tmp_path / "folders.tsv"
    ledger.write_text(
        "https://example.com/post/1\t2024-01-01\t" + HASH_A + "\tD:\\old\r\n"
        "https://example.com/post/2\tD:\\Downloads\\A\\2\r\n",
        encoding="utf-8",
    )

    rows = refresh.load(ledger)

    assert "https://example.com/post/1" not in rows
    assert rows["https://example.com/post/2"].folder == r"D:\Downloads\A\2"


def test_manifest_folder_uses_the_first_written_file(tmp_path):
    clip = tmp_path / "Creator" / "1" / "clip.jpg"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    manifest = tmp_path / "lane.files"
    manifest.write_text(str(clip), encoding="utf-8")

    assert refresh.manifest_folder(manifest) == str(clip.parent)


def test_manifest_folder_is_empty_without_a_manifest(tmp_path):
    assert refresh.manifest_folder(tmp_path / "missing.files") == ""
