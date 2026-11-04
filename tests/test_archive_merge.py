from dredge.archive import merge_pending
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


def knowledge() -> Knowledge:
    return Knowledge(
        rules={"example.com": parse_rule("example.com id=[0-9]{6,}")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )


def test_merge_grows_an_existing_row_and_fills_a_missing_size(tmp_path):
    archive = tmp_path / "downloads.tsv"
    archive.write_text("2026-01-01 00:00:00\texample.com/post/1234567\tA.jpg\t0\n",
                       encoding="utf-8")
    pending = tmp_path / "fold-pending.tsv"
    pending.write_text("2026-01-02 00:00:00\texample.com/post/1234567\tA.jpg;B.jpg\t10;20\n",
                       encoding="utf-8")
    seen: set[str] = set()
    archived: set[str] = set()

    result = merge_pending(pending, archive, seen, archived, knowledge())

    assert (result.added, result.grown) == (0, 1)
    text = archive.read_text(encoding="utf-8")
    assert "\tA.jpg;B.jpg\t10;20" in text
    assert pending.read_text(encoding="utf-8") == ""


def test_new_rows_are_added_newest_first_and_keyed(tmp_path):
    archive = tmp_path / "downloads.tsv"
    archive.write_text("2026-01-01 00:00:00\texample.com/old\tOld.mp4\t1\n", encoding="utf-8")
    pending = tmp_path / "fold-pending.tsv"
    pending.write_text("2026-01-02 00:00:00\texample.com/new\tNew.mp4\t2\n", encoding="utf-8")
    seen: set[str] = set()
    archived: set[str] = set()

    result = merge_pending(pending, archive, seen, archived, knowledge())

    assert result.added == 1
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("2026-01-02")
    assert lines[1].startswith("2026-01-01")
    assert "example.com/new" in seen


def test_a_row_for_a_url_already_recorded_is_not_duplicated(tmp_path):
    archive = tmp_path / "downloads.tsv"
    archive.write_text("2026-01-01 00:00:00\texample.com/a\tA.mp4\t1\n", encoding="utf-8")
    pending = tmp_path / "fold-pending.tsv"
    pending.write_text("2026-01-02 00:00:00\texample.com/a\tA.mp4\t1\n", encoding="utf-8")

    result = merge_pending(pending, archive, set(), set(), knowledge())

    assert result.total == 0
    assert len(archive.read_text(encoding="utf-8").splitlines()) == 1


def test_dry_run_leaves_both_files_alone(tmp_path):
    archive = tmp_path / "downloads.tsv"
    archive.write_text("", encoding="utf-8")
    pending = tmp_path / "fold-pending.tsv"
    pending.write_text("2026-01-02 00:00:00\texample.com/a\tA.mp4\t1\n", encoding="utf-8")

    result = merge_pending(pending, archive, set(), set(), knowledge(), write=False)

    assert result.added == 1
    assert pending.read_text(encoding="utf-8").strip()
