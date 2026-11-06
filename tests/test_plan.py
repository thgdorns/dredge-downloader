from dredge.plan import build_plan, split_line, with_spec
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


def knowledge() -> Knowledge:
    return Knowledge(
        rules={"example.com": parse_rule("example.com id=[0-9]{6,}")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )


def test_drops_unusable_lines():
    plan = build_plan(
        ["not a url", "ftp://example.com/x", "https://example.com/a"],
        seen=set(), failed=set(), skip_always=set(), knowledge=knowledge(),
    )
    assert plan.unusable == 2
    assert plan.urls == ["https://example.com/a"]


def test_skips_downloaded_by_canonical_form():
    plan = build_plan(
        ["https://www.example.com/a/?utm_source=x"],
        seen={"example.com/a"}, failed=set(), skip_always=set(), knowledge=knowledge(),
    )
    assert plan.downloaded == 1
    assert plan.urls == []


def test_skips_downloaded_by_site_id():
    plan = build_plan(
        ["https://example.com/post/1234567"],
        seen={"id:example.com:1234567"}, failed=set(), skip_always=set(),
        knowledge=knowledge(),
    )
    assert plan.downloaded == 1


def test_skip_txt_beats_retry_failed():
    plan = build_plan(
        ["https://example.com/post/1234567"],
        seen=set(), failed=set(),
        skip_always={"id:example.com:1234567"},
        knowledge=knowledge(), retry_failed=True,
    )
    assert plan.marked_skip == 1
    assert plan.urls == []


def test_known_failed_is_skipped_unless_retrying():
    url = "https://example.com/post/1234567"
    failed = {"example.com/post/1234567"}
    off = build_plan([url], seen=set(), failed=failed, skip_always=set(), knowledge=knowledge())
    on = build_plan(
        [url], seen=set(), failed=failed, skip_always=set(), knowledge=knowledge(),
        retry_failed=True,
    )
    assert off.failed_skip == 1 and off.urls == []
    assert on.failed_skip == 0 and on.urls == [url]


def test_duplicate_lines_are_kept_once_in_order():
    plan = build_plan(
        ["https://example.com/b", "https://example.com/a/", "https://www.example.com/a"],
        seen=set(), failed=set(), skip_always=set(), knowledge=knowledge(),
    )
    assert plan.urls == ["https://example.com/b", "https://example.com/a/"]
    assert plan.duplicates == 1


def test_records_check_off_ignores_every_record():
    url = "https://example.com/post/1234567"
    plan = build_plan(
        [url],
        seen={"example.com/post/1234567"},
        failed={"example.com/post/1234567"},
        skip_always={"example.com/post/1234567"},
        knowledge=knowledge(),
        archive_check=False,
    )
    assert plan.urls == [url]
    assert plan.skipped == 0


def test_summary_names_the_reason():
    plan = build_plan(
        ["https://example.com/post/1234567", "junk"],
        seen={"example.com/post/1234567"}, failed=set(), skip_always=set(),
        knowledge=knowledge(),
    )
    assert "1 already downloaded" in plan.summary()
    assert "1 unusable" in plan.summary()


def test_split_line_reads_a_tab_destination():
    assert split_line("https://example.com/a") == ("https://example.com/a", "")
    assert split_line("https://example.com/a\tSome Author") == (
        "https://example.com/a", "Some Author",
    )
    assert split_line("  https://example.com/a\tG:\\Where  ") == (
        "https://example.com/a", "G:\\Where",
    )
    assert with_spec("https://example.com/a", "") == "https://example.com/a"
    assert with_spec("https://example.com/a", "x") == "https://example.com/a\tx"


def test_a_queued_line_keeps_its_destination():
    plan = build_plan(
        ["https://example.com/a\tSome Author"],
        seen=set(), failed=set(), skip_always=set(), knowledge=knowledge(),
    )
    assert plan.urls == ["https://example.com/a\tSome Author"]


def test_the_same_url_with_two_destinations_is_one_line():
    plan = build_plan(
        [
            "https://example.com/a\tfirst",
            "https://example.com/a\tsecond",
        ],
        seen=set(), failed=set(), skip_always=set(), knowledge=knowledge(),
    )
    assert plan.urls == ["https://example.com/a\tfirst"]
    assert plan.duplicates == 1


def test_the_destination_is_not_part_of_the_archive_key():
    plan = build_plan(
        ["https://example.com/a\tSome Author"],
        seen={"example.com/a"}, failed=set(), skip_always=set(),
        knowledge=knowledge(),
    )
    assert plan.downloaded == 1
    assert plan.urls == []
