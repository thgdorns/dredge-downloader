from datetime import UTC, datetime

from dredge.report import domain_of, group_by_domain, render


def test_domain_of_strips_www_port_and_userinfo():
    assert domain_of("https://www.Example.com:8080/x?y=1") == "example.com"
    assert domain_of("https://user:pass@sub.example.org/a") == "sub.example.org"


def test_group_by_domain_is_sorted_and_deduped():
    lines = [
        "https://b.example/x",
        "https://a.example/y",
        "https://www.a.example/z",
        "https://a.example/y",
    ]
    groups = group_by_domain(lines)

    assert list(groups) == ["a.example", "b.example"]
    assert groups["a.example"] == ["https://a.example/y", "https://www.a.example/z"]


def test_render_escapes_and_links_every_url():
    lines = ["https://a.example/x?a=1&b=2", "https://a.example/<bad>"]
    page = render("Test", lines, datetime(2026, 11, 4, 12, 0, tzinfo=UTC))

    assert "2 unique URL(s) across 1 domain(s)" in page
    assert "generated 2026-11-04 12:00" in page
    assert "https://a.example/x?a=1&amp;b=2" in page
    assert "https://a.example/&lt;bad&gt;" in page
    assert 'href="https://a.example/x?a=1&amp;b=2"' in page
    assert 'id="d-a-example"' in page


def test_render_of_an_empty_record_says_so():
    page = render("Empty", [])
    assert "Nothing here." in page
    assert "0 unique URL(s)" in page
