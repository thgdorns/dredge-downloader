from dredge import __version__
from dredge.cli import build_parser


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


def test_version_is_reported():
    assert __version__
