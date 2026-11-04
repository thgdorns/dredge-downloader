from dredge.plan import build_plan
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
