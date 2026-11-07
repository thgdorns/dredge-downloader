#!/usr/bin/env python3
"""Scan the default download folder for URLs and passwords, write HTML reports.

This is the standalone URL hunter, wired to dredge: the folder defaults to the
default destination in ``config/dredge.conf``, the reports land in
``reports/url-scan-<stamp>.html`` and ``reports/passwords-<stamp>.html``, the
metadata it read is sent to the recycle bin afterwards (its only content of
interest is what was just extracted), and both reports are opened at the end.

By default it reads the two sidecar kinds the downloaders leave behind:

    *.description        yt-dlp's video or playlist description
    <name>.<ext>.json    pawchive/kemono metadata (photo.jpg.json)

A plain ``something.json`` (or mod data such as ``Shu.upgrades.json``) is not a
downloader sidecar and is left alone. A sidecar is read for its ``content``
field only: the ``attachments`` / ``file`` URLs in it point at the files
already on disk.

Besides URLs it collects the passwords posts hand out (``pass is xyz``,
``password: xy``, ``password is the usual``, ``解压密码：777``, ``密码是abc``,
``パスワード：テスト``, ``비밀번호: 1234``) in English, Spanish, Japanese,
Chinese and Korean.

Once a URL is in the report it has done its job, so the metadata that carried
it is sent to the recycle bin (see ``--keep-metadata``).

Duplicate values are merged into one row, with every file that contained it
joined by ``;``. A mega/mediafire/drive link is not reported at all: it is
appended to ``data/queue.txt`` as ``url<TAB>folder``, pointing at the author's
folder (the first subfolder under the scanned root), so dredge downloads it
next to the rest of that author's files.

Each HTML page is a self-contained sortable/filterable table with two columns:
    1. value -- the URL or password that was found, followed by a literal tab
       and the author's name (the first subfolder under the scan root), so a
       row can be copied into queue.txt as ``value<TAB>author``
    2. Found in -- the absolute path(s) of the file(s) that contained it

Examples:
    py tools\\url_scan.py                  # default destination, sidecars only
    py tools\\url_scan.py -e .json         # every .json, sidecar or not
    py tools\\url_scan.py -e '*'           # every file
    py tools\\url_scan.py --no-queue       # leave file-host links in the report
    py tools\\url_scan.py --keep-metadata  # do not recycle what was scanned
    py tools\\url_scan.py --no-passwords   # URLs only
    py tools\\url_scan.py --no-open -q     # headless run
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dredge.config import Paths, Settings
from dredge.destinations import apply_fallback

ROOT = Path(__file__).resolve().parent.parent

# What a default scan reads: yt-dlp descriptions and the JSON sidecars
# pawchive/kemono write next to every file.
DEFAULT_EXTENSIONS = (".description", ".json")

# Links that are queued instead of reported: each one names a file the post is
# handing out, so dredge fetches it directly into the folder the link was found
# in.
FILEHOSTS = (
    "mediafire.com", "drive.google.com", "docs.google.com", "mega.nz", "mega.io",
)

# The extension in front of ".json" has to be a real file kind: mod data named
# "Shu.upgrades.json" is not a downloader sidecar and must not be recycled.
SIDECAR_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".tif", ".tiff",
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".vob",
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".z01", ".psd", ".clip", ".pdf",
    ".txt", ".epub", ".apk",
})

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

# --------------------------------------------------------------------------
# Password detection
# --------------------------------------------------------------------------

# The words a post uses to hand over an archive password, in the five languages
# these creators write in. Bare "pass"/"code"/"clave"/"暗号" are included on
# purpose: the report is read by a human, so a false positive is cheaper than a
# missed password. Longest first so "password" wins over "pass".
_PASSWORD_HINTS = "|".join(sorted({
    # English
    "password", "passwd", "passcode", "pass", "pwd", "pw", "code", "secret",
    # Spanish
    "contraseña", "contrasena", "clave",
    # Japanese
    "パスワード", "解凍パスワード", "解凍パス", "パス", "合言葉", "暗証番号",
    # Chinese
    "解压密码", "解壓密碼", "压缩密码", "解压码", "解壓碼", "提取码", "密码", "密碼",
    # Korean
    "비밀번호", "패스워드", "비번", "암호",
}, key=len, reverse=True))

# After the hint: an optional "is/es/是/=/:…" and then the value, which runs to
# the end of the sentence so a phrase like "the usual" stays whole. The hint,
# the link word and the colon stay on one line: a label with nothing after it
# (an empty line) must not swallow the next paragraph.
# Short hints that are ordinary words on their own ("pass by", "code of
# conduct"): they only count when a separator follows ("pass is", "pass:",
# "pw="). The explicit ones may be followed by the value directly.
_BARE_HINTS = {"pass", "pwd", "pw", "code", "secret", "パス", "暗号"}

PASSWORD_RE = re.compile(
    r"(?P<hint>" + _PASSWORD_HINTS + r")(?![A-Za-z])[ \t]*"
    r"(?:(?P<link>is|are|was|were|es|son|está|esta|是|为|為)|(?P<eq>=|＝))?[ \t]*"
    r"(?P<colon>[:：])?[ \t]*"
    r"(?P<value>[^\s<>{}\[\]()\"'|;:：、。\n][^<>{}\[\]()\"'|;、。\n]{0,60})",
    re.IGNORECASE,
)


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


# Values that describe the password instead of being one ("password protected
# archive", "password is required"). "the usual" is deliberately not here.
_PASSWORD_STOPWORDS = {
    "protected", "required", "needed", "incorrect", "wrong", "unknown", "private",
}

# "...del archivo: abc123" / "...del zip es 4321": the value is after the last
# separator. A colon with no space after it (https://…) is left alone.
_PASSWORD_SPLIT = re.compile(r"(?:[:：]|\b(?:es|is|son|are)\b)\s+")


def extract_passwords(text: str) -> list[str]:
    """Passwords hinted at in ``text``, in order of first appearance.

    ``pass is xyz``, ``password: xy``, ``password is xyz``, ``pass is the
    usual``, ``解压密码：777``, ``密码是abc``, ``パスワード：テスト``,
    ``비밀번호: 1234`` all yield the value after the hint, phrase included.
    """
    seen: set[str] = set()
    found: list[str] = []
    for match in PASSWORD_RE.finditer(text):
        hint = match.group("hint").lower()
        separator = bool(
            match.group("link") or match.group("eq") or match.group("colon")
        )
        if hint in _BARE_HINTS and not separator:
            continue
        value = match.group("value")
        # "clave del archivo: abc123" / "contraseña del zip es 4321": the value
        # follows the last separator, the words before it name the archive
        parts = _PASSWORD_SPLIT.split(value)
        if len(parts) > 1 and parts[-1].strip():
            value = parts[-1]
        # "777. Enjoy!" - the sentence ends at the full stop
        value = re.split(r"(?<=[.!?。！？])\s+", value, maxsplit=1)[0]
        value = value.strip().strip("*`").strip().strip(".,;:!?。").strip()
        if not value:
            continue
        if value.split()[0].lower().strip(".,;:") in _PASSWORD_STOPWORDS:
            continue
        if value in seen:
            continue
        seen.add(value)
        found.append(value)
    return found


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


def is_sidecar(path: Path) -> bool:
    """``photo.jpg.json`` and kemono's ``post.json`` are metadata sidecars."""
    if path.suffix.lower() != ".json":
        return False
    if path.name.lower() == "post.json":
        return True
    suffixes = path.suffixes
    if len(suffixes) < 2:
        return False
    return suffixes[-2].lower() in SIDECAR_EXTENSIONS


def json_content(text: str) -> str | None:
    """The post body of a kemono/pawchive sidecar, or ``None`` when it has none.

    Those sidecars carry the post's own text in ``content`` and every downloaded
    file in ``attachments``/``file``; only the body can hold links the user
    wants back, the attachments are the files themselves.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    body = data.get("content")
    return body if isinstance(body, str) else None


def iter_files(
    root: Path,
    exts: set[str] | None,
    recursive: bool,
    exclude: list[str],
    sidecar_only: bool = False,
) -> list[Path]:
    walker = root.rglob("*") if recursive else root.glob("*")
    files: list[Path] = []
    for path in walker:
        if not path.is_file():
            continue
        if exts is not None and path.suffix.lower() not in exts:
            continue
        if sidecar_only and path.suffix.lower() == ".json" and not is_sidecar(path):
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


def is_filehost(url: str) -> bool:
    """True for the links that belong in the queue, not in the report."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in FILEHOSTS)


def merge_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One row per value; the files it was found in are joined with ``;``."""
    merged: dict[str, list[str]] = {}
    for value, path in rows:
        paths = merged.setdefault(value, [])
        if path not in paths:
            paths.append(path)
    return [(value, ";".join(paths)) for value, paths in merged.items()]


def author_of(path: Path, root: Path) -> str:
    """The first folder under the scan root, or ``""`` for a root file."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ""
    return relative.parts[0] if len(relative.parts) > 1 else ""


def author_for(paths: str, root: Path | None) -> str:
    """The first author among a row's source paths, or ``""``."""
    if root is None:
        return ""
    for raw in paths.split(";"):
        if not raw:
            continue
        author = author_of(Path(raw), root)
        if author:
            return author
    return ""


def queue_links(path: Path, rows: list[tuple[str, str]]) -> tuple[int, int]:
    """Append ``url<TAB>folder`` lines; returns ``(added, already queued)``."""
    if not rows:
        return 0, 0
    existing: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            existing.add(line.split("\t", 1)[0].strip())
    lines: list[str] = []
    skipped = 0
    for url, folder in rows:
        if not folder or url in existing:
            skipped += 1
            continue
        existing.add(url)
        lines.append(f"{url}\t{folder}")
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as handle:
            for line in lines:
                handle.write(line + "\r\n")
    return len(lines), skipped


_REPORT_JS = """
(function () {
  "use strict";
  const table = document.getElementById("t");
  const tbody = table.tBodies[0];
  const all = Array.from(tbody.rows);
  const q = document.getElementById("q");
  let key = "value";
  let dir = 1;
  function apply() {
    const needle = q.value.trim().toLowerCase();
    for (const row of all) {
      row.style.display =
        !needle || row.innerText.toLowerCase().includes(needle) ? "" : "none";
    }
    const idx = key === "value" ? 0 : 1;
    const sorted = all.slice().sort(function (a, b) {
      const x = a.cells[idx].innerText.toLowerCase();
      const y = b.cells[idx].innerText.toLowerCase();
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
    for (const row of sorted) tbody.appendChild(row);
  }
  document.querySelectorAll("th[data-key]").forEach(function (th) {
    th.addEventListener("click", function () {
      if (key === th.dataset.key) dir = -dir;
      else { key = th.dataset.key; dir = 1; }
      document.querySelectorAll("th[data-key]").forEach(function (other) {
        other.classList.remove("sorted", "asc", "desc");
      });
      th.classList.add("sorted", dir === 1 ? "asc" : "desc");
      apply();
    });
  });
  q.addEventListener("input", apply);
  document.getElementById("reset").addEventListener("click", function () {
    q.value = "";
    key = "value";
    dir = 1;
    document.querySelectorAll("th[data-key]").forEach(function (th) {
      th.classList.remove("sorted", "asc", "desc");
    });
    apply();
  });
  apply();
})();
"""


def _value_html(value: str) -> str:
    """A URL as a link, a password as inline code."""
    safe = html.escape(value, quote=True)
    if value.lower().startswith(("http://", "https://")):
        return f'<a href="{safe}" rel="noreferrer">{safe}</a>'
    return f"<code>{safe}</code>"


def render_report(
    title: str,
    rows: list[tuple[str, str]],
    *,
    files: int,
    generated: str | None = None,
    root: Path | None = None,
) -> str:
    """One self-contained sortable/filterable HTML table.

    When ``root`` is the scan folder, each value is followed by a literal tab
    and the first subfolder it came from (the author), so the cell can be
    copied straight into ``queue.txt`` as ``value<TAB>author``.
    """
    stamp = generated or time.strftime("%Y-%m-%d %H:%M")
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        (
            ":root{color-scheme:light dark}"
            "body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:1400px;margin:2rem auto;"
            "padding:0 1rem;color:#1d1d1f;background:#fff}"
            "h1{font-size:1.5rem;margin:0 0 .25rem}"
            ".meta{color:#666;margin:0 0 1rem}"
            ".controls{display:flex;flex-wrap:wrap;gap:.6rem;align-items:end;margin:1rem 0}"
            ".controls input{font:inherit;padding:.3rem .45rem;border:1px solid #c9c9cf;"
            "border-radius:6px;background:transparent;color:inherit;min-width:22rem}"
            ".controls button{font:inherit;padding:.3rem .7rem;border:1px solid #c9c9cf;"
            "border-radius:6px;background:transparent;color:inherit}"
            "table{border-collapse:collapse;width:100%}"
            "th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #e4e4e8;"
            "vertical-align:top}"
            "th{position:sticky;top:0;background:#fff;cursor:pointer;user-select:none;"
            "white-space:nowrap}"
            "th.sorted.asc::after{content:' \\25B2'}th.sorted.desc::after{content:' \\25BC'}"
            "td.value{white-space:pre-wrap;word-break:break-all}"
            "td.paths{color:#555;word-break:break-word}"
            ".author{color:#666;font-size:.9em}"
            ".hint{color:#666;margin:-.6rem 0 1rem;font-size:.9rem}"
            "a{color:#0645ad}code{font-size:.95em}"
            "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}"
            "th{background:#111}th,td{border-color:#333}.controls input,.controls button{"
            "border-color:#555}a{color:#8ab4f8}td.paths{color:#aaa}"
            ".author,.hint{color:#aaa}}"
        ),
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta">{len(rows)} unique value(s) from {files} file(s); generated {stamp}</p>',
        '<p class="hint">Copy a row\'s value as <code>value&lt;TAB&gt;author</code> for queue.txt.</p>',
        '<div class="controls">',
        '<input id="q" type="search" placeholder="Filter value or path..." aria-label="Filter">',
        '<button id="reset" type="button">Reset</button>',
        "</div>",
        '<table id="t"><thead><tr>',
        '<th data-key="value">Value</th><th data-key="paths">Found in</th>',
        "</tr></thead><tbody>",
    ]
    for value, paths in rows:
        cells = "".join(
            f'<div class="path">{html.escape(path)}</div>'
            for path in paths.split(";") if path
        )
        value_html = _value_html(value)
        author = author_for(paths, root)
        if author:
            value_html += f'<span class="author">\t{html.escape(author)}</span>'
        parts.append(
            '<tr class="row">'
            f'<td class="value">{value_html}</td>'
            f'<td class="paths">{cells}</td>'
            "</tr>"
        )
    parts.extend([
        "</tbody></table>",
        "<script>",
        _REPORT_JS,
        "</script>",
        "</body></html>",
    ])
    return "\n".join(parts) + "\n"


def write_html(
    path: Path,
    title: str,
    rows: list[tuple[str, str]],
    *,
    files: int,
    root: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_report(title, rows, files=files, root=root),
        encoding="utf-8",
        newline="",
    )


# --------------------------------------------------------------------------
# Dredge wiring
# --------------------------------------------------------------------------

def default_destination(root: Path) -> Path:
    """The default destination in config/dredge.conf, fallback drive included."""
    settings = apply_fallback(Settings.load(Paths(root)))
    return settings.destinations[settings.default_dest].path


def open_report(path: Path) -> None:
    """Open the HTML report in the desktop's browser."""
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
# Recycle bin
# --------------------------------------------------------------------------

def _recycle_windows(paths: list[Path]) -> tuple[int, list[str]]:
    """SHFileOperation with FOF_ALLOWUNDO: the Recycle Bin, never a cold delete."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),          # FILEOP_FLAGS is a WORD
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    done = 0
    failed: list[str] = []
    for start in range(0, len(paths), 200):
        chunk = paths[start:start + 200]
        listing = "\0".join(str(p) for p in chunk) + "\0\0"
        source = ctypes.create_unicode_buffer(listing)
        op = SHFILEOPSTRUCTW(
            None, FO_DELETE, ctypes.cast(source, wintypes.LPCWSTR), None,
            flags, False, None, None,
        )
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if code == 0 and not op.fAnyOperationsAborted:
            done += len(chunk)
        else:
            failed.extend(str(p) for p in chunk)
    return done, failed


def _recycle_posix(paths: list[Path]) -> tuple[int, list[str]]:
    """``gio trash`` (GNOME/KDE) or ``trash-put``; nothing when neither exists."""
    tool = None
    for name in ("gio", "trash-put"):
        if shutil.which(name):
            tool = name
            break
    if tool is None:
        return 0, [str(p) for p in paths]

    done = 0
    failed: list[str] = []
    for path in paths:
        argv = (
            ["gio", "trash", "--", str(path)] if tool == "gio"
            else ["trash-put", "--", str(path)]
        )
        try:
            subprocess.run(argv, check=False, capture_output=True)
        except OSError:
            pass
        if path.exists():
            failed.append(str(path))
        else:
            done += 1
    return done, failed


def recycle(paths: list[Path]) -> tuple[int, list[str]]:
    """Send files to the desktop's recycle bin / trash. Never deletes outright."""
    if not paths:
        return 0, []
    if sys.platform == "win32":
        return _recycle_windows(paths)
    return _recycle_posix(paths)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively find URLs in files and write them to a "
                    "sortable HTML report (value + absolute file path).",
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
        help="output HTML path (default: reports/url-scan-<stamp>.html)",
    )
    parser.add_argument(
        "-e", "--ext", "--extensions", nargs="+", default=None,
        dest="ext",
        help="file extensions to scan (default: .description plus the "
             "<name>.<ext>.json sidecars pawchive/kemono write). Pass "
             "'-e .json' to scan every .json, or '*' for every file",
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
        "--no-queue", action="store_true",
        help="do not add mega/mediafire/drive links to queue.txt (default: "
             "they are queued into the folder they were found in and dropped "
             "from the report)",
    )
    parser.add_argument(
        "--no-sort", action="store_true",
        help="write rows in scan order instead of sorting by path then URL",
    )
    parser.add_argument(
        "--keep-metadata", action="store_true",
        help="keep the scanned .json/.description files (default: send them "
             "to the recycle bin)",
    )
    parser.add_argument(
        "--no-passwords", action="store_true",
        help="do not look for password hints (default: also write "
             "passwords-<stamp>.html)",
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
        if output.suffix.lower() not in (".html", ".htm"):
            output = output.with_suffix(".html")
        stamp = time.strftime("%Y%m%d-%H%M%S")
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = root / "reports" / f"url-scan-{stamp}.html"
    passwords_output = output.parent / f"passwords-{stamp}.html"

    if args.ext is None:
        exts: set[str] | None = set(DEFAULT_EXTENSIONS)
        sidecar_only = True
    else:
        exts = normalise_extensions(args.ext)
        sidecar_only = False
    files = iter_files(
        folder, exts, not args.no_recursive, args.exclude, sidecar_only
    )

    rows: list[tuple[str, str]] = []
    password_rows: list[tuple[str, str]] = []
    scanned: list[Path] = []
    skipped_large = 0
    skipped_binary = 0

    for path in files:
        if path in (output, passwords_output):
            continue
        text = read_file(path, args.encoding, args.max_size, args.include_binary)
        if text is None:
            if args.max_size is not None and path.stat().st_size > args.max_size:
                skipped_large += 1
            elif not args.include_binary:
                skipped_binary += 1
            continue
        scanned.append(path)

        # A kemono/pawchive sidecar is read for its post body only: the
        # attachment URLs in it point at the files already on disk.
        if path.suffix.lower() == ".json":
            body = json_content(text)
            if body is not None:
                text = body

        if args.json_escapes:
            text = decode_json_escapes(text)

        full_path = str(path)
        for url in extract_urls(text):
            if args.html_entities:
                url = html.unescape(url)
            rows.append((url, full_path))

        if not args.no_passwords:
            for value in extract_passwords(text):
                password_rows.append((value, full_path))

    table = merge_rows(rows)
    password_table = merge_rows(password_rows)
    if not args.no_sort:
        table.sort(key=lambda row: (row[1].split(";", 1)[0].lower(), row[0]))
        password_table.sort(
            key=lambda row: (row[1].split(";", 1)[0].lower(), row[0])
        )

    # File-host links are the point of the scan: they are queued into the
    # author folder they were found under (the first subfolder beneath the scan
    # root), and leave the report.
    report_rows: list[tuple[str, str]] = []
    queue_rows: list[tuple[str, str]] = []
    for url, paths in table:
        if args.no_queue or not is_filehost(url):
            report_rows.append((url, paths))
            continue
        author = author_for(paths, folder)
        destination = folder / author if author else folder
        queue_rows.append((url, str(destination)))

    write_html(output, "URL scan", report_rows, files=len(scanned), root=folder)
    if not args.no_passwords:
        write_html(passwords_output, "Passwords", password_table, files=len(scanned))

    queued = 0
    already = 0
    if not args.no_queue:
        queue_path = Paths(root).queue
        queued, already = queue_links(queue_path, queue_rows)
        queued_path = queue_path

    trashed = 0
    leftovers: list[str] = []
    if not args.keep_metadata:
        metadata = [
            path for path in scanned
            if path.suffix.lower() in (".json", ".description")
        ]
        trashed, leftovers = recycle(metadata)

    if not args.quiet:
        print(
            f"scanned {len(files)} file(s) under {folder}, found "
            f"{len(table)} URL(s) in {len(rows)} row(s)"
        )
        print(f"report: {output}")
        if queued or queue_rows:
            print(
                f"queued {queued} file-host link(s) into {queued_path}"
                + (f" ({already} already queued)" if already else "")
            )
        if not args.no_passwords:
            print(f"passwords: {len(password_table)} -> {passwords_output}")
        if trashed:
            print(f"moved {trashed} metadata file(s) to the recycle bin")
        if leftovers:
            print(
                f"could not recycle {len(leftovers)} file(s) - left in place",
                file=sys.stderr,
            )
        if skipped_binary:
            print(f"skipped {skipped_binary} binary file(s)")
        if skipped_large:
            print(f"skipped {skipped_large} oversized file(s)")

    if not args.no_open:
        open_report(output)
        if not args.no_passwords:
            open_report(passwords_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
