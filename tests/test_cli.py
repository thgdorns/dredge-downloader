from dredge import __version__, cli, records
from dredge.cli import build_parser, sort_records
from dredge.config import Paths


def test_legacy_flags_are_accepted():
    args = build_parser().parse_args(
        ["--once", "--dry-run", "--retry-failed", "--max-downloads", "6", "--root", "."]
    )
    assert args.once and args.dry_run and args.retry_failed
    assert args.max_downloads == 6
    assert args.root == "."


def test_worker_flags():
    args = build_parser().parse_args(
        ["--worker", "--url", "https://example.com/a", "--dest", "out",
         "--lane-log", "l.log", "--result", "r.txt", "--archive", "a.tsv"]
    )
    assert args.worker
    assert args.url == "https://example.com/a"
    assert args.archive == "a.tsv"


def test_refresh_takes_one_or_more_post_urls():
    args = build_parser().parse_args(
        ["--refresh", "https://example.com/post/1",
         "--refresh", "https://example.com/post/2"]
    )

    assert args.refresh == [
        "https://example.com/post/1",
        "https://example.com/post/2",
    ]


def test_console_replaces_unencodable_filenames(monkeypatch):
    class Stream:
        def __init__(self):
            self.options = {}

        def reconfigure(self, **kwargs):
            self.options.update(kwargs)

    out, err = Stream(), Stream()
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)

    cli._safe_console()

    assert out.options == {"errors": "replace"}
    assert err.options == {"errors": "replace"}


def test_version_is_reported():
    assert __version__


def test_startup_sorts_the_record_lists(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    records.write_lines(paths.queue, ["https://b", "https://a"])
    records.write_lines(paths.fail, ["https://d", "https://c"])
    records.write_lines(paths.offline, ["https://f", "https://e"])
    paths.skip.write_text(
        "# keep this\n\nhttps://z\nhttps://A\n", encoding="utf-8"
    )

    sort_records(paths)

    assert records.read_lines(paths.queue) == ["https://a", "https://b"]
    assert records.read_lines(paths.fail) == ["https://c", "https://d"]
    assert records.read_lines(paths.offline) == ["https://e", "https://f"]
    assert records.read_lines(paths.skip) == ["https://A", "https://z"]
    assert paths.skip.read_bytes().startswith(b"# keep this\r\n")

