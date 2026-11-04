from dredge.cookies import drop_blocked, read_cookie_names
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge

HEADER = "# Netscape HTTP Cookie File"


def knowledge() -> Knowledge:
    return Knowledge(
        rules={"youtube.com": parse_rule("youtube.com cookiedrop=ST-*")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )


def test_cookiedrop_removes_only_the_named_cookies():
    lines = [
        HEADER,
        "youtube.com\tTRUE\t/\tTRUE\t0\tST-abc\tsecret",
        "youtube.com\tTRUE\t/\tTRUE\t0\tSID\tkeep",
        "example.com\tTRUE\t/\tTRUE\t0\tST-abc\tkeep",
    ]
    kept, dropped = drop_blocked(lines, knowledge())
    assert dropped == 1
    assert any(line.endswith("\tSID\tkeep") for line in kept)
    assert any(line.startswith("example.com") for line in kept)
    assert len(kept) == 3


def test_httponly_lines_are_real_cookies_not_comments():
    lines = ["#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tST-x\tv"]
    kept, dropped = drop_blocked(lines, knowledge())
    assert dropped == 1 and kept == []


def test_comments_and_short_lines_survive():
    lines = [HEADER, "# a comment", "broken line"]
    kept, dropped = drop_blocked(lines, knowledge())
    assert dropped == 0 and kept == lines


def test_cookie_names_reads_both_forms(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        HEADER + "\n"
        "youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv\n"
        "#HttpOnly_.fantia.jp\tTRUE\t/\tTRUE\t0\t_session_id\tv\n",
        encoding="utf-8",
    )
    assert read_cookie_names(path) == {"SID", "_session_id"}
