from datetime import UTC, datetime

from dredge import report
from dredge.report import (
    DOWNLOAD_DATA,
    DOWNLOAD_STATE,
    domain_of,
    group_by_domain,
    render,
    write_reports,
)


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


def test_a_tab_destination_is_not_part_of_the_url():
    groups = group_by_domain([
        "https://a.example/x\tG:\\Downloads\\Some Author",
        "https://a.example/y\tSome Author",
    ])

    assert groups == {"a.example": ["https://a.example/x", "https://a.example/y"]}


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

def _root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "downloads.tsv").write_text(
        "2026-11-06 10:00:00\ta.example/x\tfile one.mp4\t100\n"
        "2026-11-05 09:00:00\tb.example/y\tfile two.mp4\t200\n",
        encoding="utf-8", newline="",
    )
    for name in ("fail", "skip", "offline"):
        (data / f"{name}.txt").write_text(
            f"https://{name}.example/z\n", encoding="utf-8", newline=""
        )
    return tmp_path


def test_write_reports_writes_all_four_pages(tmp_path):
    root = _root(tmp_path)

    written = write_reports(root)

    assert list(written) == ["fail", "skip", "offline", "downloads"]
    for path in written.values():
        assert path.exists()
    assert "Skipped links" in written["skip"].read_text(encoding="utf-8")
    data = (root / "reports" / DOWNLOAD_DATA).read_text(encoding="utf-8")
    assert data.count("push(") == 2
    assert "file one.mp4" in data


def test_downloads_data_is_append_only(tmp_path):
    root = _root(tmp_path)
    archive = root / "data" / "downloads.tsv"
    data = root / "reports" / DOWNLOAD_DATA

    write_reports(root)
    first = data.read_text(encoding="utf-8")

    write_reports(root)
    assert data.read_text(encoding="utf-8") == first

    old = archive.read_text(encoding="utf-8")
    archive.write_text(
        "2026-11-07 11:00:00\tc.example/z\tfile three.mp4\t300\n" + old,
        encoding="utf-8", newline="",
    )
    write_reports(root)
    second = data.read_text(encoding="utf-8")

    assert second.startswith(first)
    assert second.count("push(") == 3
    assert "file three.mp4" in second[len(first):]


def test_a_grown_row_is_pushed_again(tmp_path):
    root = _root(tmp_path)
    archive = root / "data" / "downloads.tsv"

    write_reports(root)
    lines = archive.read_text(encoding="utf-8").splitlines()
    lines[0] = "2026-11-06 10:00:00\ta.example/x\tfile one.mp4;extra.mp4\t100;50"
    archive.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    write_reports(root)
    data = (root / "reports" / DOWNLOAD_DATA).read_text(encoding="utf-8")

    assert data.count("push(") == 3
    assert "extra.mp4" in data


def test_rebuild_starts_the_data_file_over(tmp_path):
    root = _root(tmp_path)
    write_reports(root)

    write_reports(root, rebuild=True)

    data = (root / "reports" / DOWNLOAD_DATA).read_text(encoding="utf-8")
    state = (root / "reports" / DOWNLOAD_STATE).read_text(encoding="utf-8")
    assert data.count("push(") == 2
    assert len(state.split()) == 2


def test_the_downloads_shell_has_sort_and_filter_controls(tmp_path):
    root = _root(tmp_path)
    page = write_reports(root)["downloads"].read_text(encoding="utf-8")

    for needle in (
        'id="q"', 'id="from"', 'id="to"',
        'data-key="date"', 'data-key="url"', 'data-key="name"',
        "downloads-data.js",
    ):
        assert needle in page


def test_open_reports_opens_every_page(tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(report.webbrowser, "open", lambda url: opened.append(url) or True)

    report.open_reports({"a": tmp_path / "a.html", "b": tmp_path / "b.html"})

    assert len(opened) == 2
    assert all(url.startswith("file:") for url in opened)