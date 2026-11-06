#!/usr/bin/env python3
"""Scan the default download folder for URLs and write a TSV report.

This is the standalone URL hunter, wired to dredge: the folder defaults to the
default destination in ``config/dredge.conf``, the report lands in
``reports/url-scan-<stamp>.tsv``, the ``.json`` metadata files it read are
deleted afterwards (their only content of interest is the URLs just extracted),
and the report is opened at the end.

Output columns:
    1. url   -- the URL that was found
    2. path  -- absolute path of the file that contained it

Examples:
    py tools\\url_scan.py                  # default destination, .json only
    py tools\\url_scan.py -e .json .txt    # other extensions as well
    py tools\\url_scan.py --keep-json      # do not delete what was scanned
    py tools\\url_scan.py --no-open -q     # headless run
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dredge.config import Paths, Settings
from dredge.destinations import apply_fallback

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# URL detection
# --------------------------------------------------------------------------

# Anything with a scheme, e.g. https://, ftp://, s3://, postgres://, ws://
# plus mailto: links. Stops at whitespace, quotes, angle brackets, JSON
# escapes that we did not decode, pipes (markdown tables) and backticks.
_SCHEME_URL = r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>\\^`|]+"
_MAILTO_URL = r"mailto:[^\s\"'<>\\^`|]+"
URL_RE = re.compile(f"(?:{_SCHEME_URL})|(?:{_MAILTO_URL})", re.IGNORECASE)

# Punctuation that is far more likely to be sentence/markdown noise than
# part of the URL itself.
_TRAILING_JUNK = ".,;:!?'\"\u201d\u2019\u00bb)]}>*"


def _clean_url(url: str) -> str:
    """Drop trailing punctuation, keeping balanced brackets like /wiki/Foo_(bar)."""
    while url:
        last = url[-1]
        if last not in _TRAILING_JUNK:
            break
        if last == ")" and url.count("(") >= url.count(")"):
            break
        if last == "]" and url.count("[") >= url.count("]"):
            break
        if last == "}" and url.count("{") >= url.count("}"):
            break
        url = url[:-1]
    return url


def decode_json_escapes(text: str) -> str:
    r"""Turn JSON-style escapes into characters so URLs are found intact.

    Handles ``\/`` -> ``/`` and ``\uXXXX`` (including ``\u0026`` -> ``&``).
    Lone surrogates are dropped; they never belong in a URL.
    """
    if "\\" not in text:
        return text
    text = text.replace("\\/", "/")

    def repl(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        if 0xD800 <= codepoint <= 0xDFFF:
            return ""
        return chr(codepoint)

    return re.sub(r"\\u([0-9a-fA-F]{4})", repl, text)


def extract_urls(text: str) -> list[str]:
    """Return every distinct URL in ``text``, in order of first appearance."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.finditer(text):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


# --------------------------------------------------------------------------
# File discovery / reading
# --------------------------------------------------------------------------

def normalise_extensions(values: list[str]) -> set[str] | None:
    """Normalise ``.json``/``json``/``*.json`` style values. None means "all"."""
    exts: set[str] = set()
    for value in values:
        for part in value.replace(",", " ").split():
            part = part.strip().lower()
            if part in {"*", "all", ".*"}:
                return None
            if not part.startswith("."):
                part = "." + part
            exts.add(part)
    return exts or None


def iter_files(
    root: Path,
    exts: set[str] | None,
    recursive: bool,
    exclude: list[str],
) -> list[Path]:
    walker = root.rglob("*") if recursive else root.glob("*")
    files: list[Path] = []
    for path in walker:
        if not path.is_file():
            continue
        if exts is not None and path.suffix.lower() not in exts:
            continue
        if any(path.match(pattern) for pattern in exclude):
            continue
        files.append(path)
    files.sort(key=lambda p: str(p).lower())
    return files


def decode_bytes(data: bytes, encoding: str) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for enc in (encoding, "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def read_file(
    path: Path,
    encoding: str,
    max_size: int | None,
    include_binary: bool,
) -> str | None:
    try:
        size = path.stat().st_size
        if max_size is not None and size > max_size:
            return None
        data = path.read_bytes()
    except OSError as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        return None

    if not include_binary:
        utf16 = data[:2] in (b"\xff\xfe", b"\xfe\xff")
        if not utf16 and looks_binary(data):
            return None
    return decode_bytes(data, encoding)


def sanitise_field(value: str) -> str:
    """Keep each TSV row on one line and in exactly two columns."""
    return value.replace("\t", "%09").replace("\r", "").replace("\n", "")


# --------------------------------------------------------------------------
# Dredge wiring
# --------------------------------------------------------------------------

def default_destination(root: Path) -> Path:
    """The default destination in config/dredge.conf, fallback drive included."""
    settings = apply_fallback(Settings.load(Paths(root)))
    return settings.destinations[settings.default_dest].path


def open_report(path: Path) -> None:
    """Open the TSV with whatever the desktop uses for it."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path), "open")  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError as exc:
        print(f"warning: could not open {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively find URLs in files and write them to a TSV "
                    "(column 1: url, column 2: absolute file path).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if "Examples:" in __doc__ else None,
    )
    parser.add_argument(
        "--root", default=str(ROOT),
        help="dredge root holding config/ and reports/ (default: the folder "
             "above this script)",
    )
    parser.add_argument(
        "folder", nargs="?", default=None,
        help="folder to scan (default: the default destination in dredge.conf)",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="output TSV path (default: reports/url-scan-<stamp>.tsv)",
    )
    parser.add_argument(
        "-e", "--ext", "--extensions", nargs="+", default=[".json"],
        dest="ext",
        help="file extensions to scan, e.g. -e .json .txt (default: .json; "
             "use '*' for every file)",
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="only scan the top level of the folder",
    )
    parser.add_argument(
        "--exclude", nargs="+", default=[], metavar="GLOB",
        help="glob patterns to skip, matched against the full path "
             "(e.g. --exclude '*/node_modules/*' '*.min.js')",
    )
    parser.add_argument(
        "--include-binary", action="store_true",
        help="do not skip files that look binary (contain NUL bytes)",
    )
    parser.add_argument(
        "--max-size", type=int, default=None, metavar="BYTES",
        help="skip files larger than this many bytes",
    )
    parser.add_argument(
        "--encoding", default="utf-8-sig",
        help="encoding to try first (default: utf-8-sig; falls back to "
             "utf-8, cp1252, latin-1)",
    )
    parser.add_argument(
        "--json-escapes", dest="json_escapes", action="store_true", default=True,
        help="decode JSON-style escapes like \\/ and \\u0026 (default: on)",
    )
    parser.add_argument(
        "--no-json-escapes", dest="json_escapes", action="store_false",
        help="do not decode JSON-style escapes",
    )
    parser.add_argument(
        "--html-entities", action="store_true",
        help="convert HTML entities in found URLs (&amp; -> &)",
    )
    parser.add_argument(
        "--keep-duplicates", action="store_true",
        help="keep repeated URLs from the same file (default: deduplicate "
             "per file)",
    )
    parser.add_argument(
        "--no-sort", action="store_true",
        help="write rows in scan order instead of sorting by path then URL",
    )
    parser.add_argument(
        "--keep-json", action="store_true",
        help="keep the scanned .json files (default: delete them once their "
             "URLs are in the report)",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="do not open the report when the scan finishes",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="do not print the summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()

    if args.folder:
        folder = Path(args.folder).expanduser()
    else:
        folder = default_destination(root)
    if not folder.exists():
        print(f"error: no such folder: {folder}", file=sys.stderr)
        return 2
    folder = folder.resolve()

    if args.output:
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = (root / output).resolve()
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = root / "reports" / f"url-scan-{stamp}.tsv"

    exts = normalise_extensions(args.ext)
    files = iter_files(folder, exts, not args.no_recursive, args.exclude)

    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    scanned: list[Path] = []
    skipped_large = 0
    skipped_binary = 0

    for path in files:
        if path == output:
            continue
        text = read_file(path, args.encoding, args.max_size, args.include_binary)
        if text is None:
            if args.max_size is not None and path.stat().st_size > args.max_size:
                skipped_large += 1
            elif not args.include_binary:
                skipped_binary += 1
            continue
        scanned.append(path)

        if args.json_escapes:
            text = decode_json_escapes(text)

        full_path = str(path)
        urls = extract_urls(text)
        for url in urls:
            if args.html_entities:
                url = html.unescape(url)
            key = (url, full_path)
            if not args.keep_duplicates:
                if key in seen:
                    continue
                seen.add(key)
            rows.append(key)

    if not args.no_sort:
        rows.sort(key=lambda row: (row[1].lower(), row[0]))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(
            handle, delimiter="\t", lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        for url, full_path in rows:
            writer.writerow([sanitise_field(url), sanitise_field(full_path)])

    deleted = 0
    if not args.keep_json:
        for path in scanned:
            if path.suffix.lower() != ".json":
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                print(f"warning: cannot delete {path}: {exc}", file=sys.stderr)

    if not args.quiet:
        print(
            f"scanned {len(files)} file(s) under {folder}, found {len(rows)} "
            f"URL(s) in {len({r[1] for r in rows})} file(s)"
        )
        print(f"report: {output}")
        if deleted:
            print(f"deleted {deleted} .json file(s)")
        if skipped_binary:
            print(f"skipped {skipped_binary} binary file(s)")
        if skipped_large:
            print(f"skipped {skipped_large} oversized file(s)")

    if not args.no_open:
        open_report(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
