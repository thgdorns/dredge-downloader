"""Render the record lists as HTML.

``fail.txt``, ``skip.txt`` and ``offline.txt`` become one page each, grouped by
domain and rewritten every run. ``downloads.tsv`` becomes a sortable, filterable
table: its shell is rewritten (a few KB), while the rows live in an append-only
``downloads-data.js`` beside it, so a 160k-row archive only pays for the rows
added since the last report. Each page is plain HTML/CSS/JS - no CDN, no build.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import json
import webbrowser
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .archive import format_row, read_rows
from .records import read_lines

DOWNLOAD_DATA = "downloads-data.js"
DOWNLOAD_STATE = "downloads.state"
DOWNLOADS_PAGE = "downloads.html"

_LIST_PAGES = (
    ("fail", "Failed links"),
    ("skip", "Skipped links"),
    ("offline", "Offline (dead) links"),
)


def domain_of(url: str) -> str:
    """Lowercase host without ``www.``; ``(unparsed)`` for anything else."""
    text = url.strip()
    if not text:
        return "(unparsed)"
    if "://" not in text:
        text = "http://" + text
    try:
        name = urlsplit(text).hostname or ""
    except ValueError:
        return "(unparsed)"
    return name.lower().removeprefix("www.") or "(unparsed)"


def group_by_domain(lines: list[str]) -> dict[str, list[str]]:
    """Domain -> unique URLs, both sorted case-insensitively."""
    seen: set[str] = set()
    groups: dict[str, list[str]] = defaultdict(list)
    for raw in lines:
        # a queue line may carry its destination after a tab; the report is
        # about the link
        url = raw.split("\t", 1)[0].strip()
        if not url or url in seen:
            continue
        seen.add(url)
        groups[domain_of(url)].append(url)
    ordered: dict[str, list[str]] = {}
    for domain in sorted(groups, key=str.lower):
        ordered[domain] = sorted(groups[domain], key=str.lower)
    return ordered


def _slug(domain: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in domain).strip("-") or "domain"


def render(title: str, lines: list[str], generated: datetime | None = None) -> str:
    groups = group_by_domain(lines)
    unique = sum(len(urls) for urls in groups.values())
    stamp = (generated or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        (
            ":root{color-scheme:light dark}"
            "body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;"
            "padding:0 1rem;color:#1d1d1f;background:#fff}"
            "h1{font-size:1.5rem;margin:0 0 .25rem}h2{font-size:1.05rem;margin:2rem 0 .4rem}"
            "h2 span{font-weight:400;color:#666;font-size:.85rem}"
            ".meta{color:#666;margin:0 0 1.25rem}"
            ".index{display:flex;flex-wrap:wrap;gap:.4rem;margin:1rem 0 2rem;padding:0;list-style:none}"
            ".index a{display:inline-block;padding:.15rem .55rem;border:1px solid #d0d0d5;"
            "border-radius:999px;text-decoration:none;color:#0645ad;font-size:.9rem}"
            ".index a small{color:#666}"
            "ul.urls{margin:0;padding-left:1.1rem}li{margin:.15rem 0;word-break:break-all}"
            "a{color:#0645ad}code{font-size:.9em}"
        ),
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        (
            f'<p class="meta">{unique} unique URL(s) across {len(groups)} domain(s); '
            f"generated {stamp}</p>"
        ),
    ]
    if not groups:
        parts.append("<p>Nothing here.</p>")
    else:
        parts.append('<ul class="index">')
        for domain, urls in groups.items():
            parts.append(
                f'<li><a href="#d-{_slug(domain)}">{html.escape(domain)} '
                f"<small>{len(urls)}</small></a></li>"
            )
        parts.append("</ul>")
        for domain, urls in groups.items():
            parts.append(
                f'<section id="d-{_slug(domain)}"><h2>{html.escape(domain)} '
                f"<span>{len(urls)}</span></h2><ul class=\"urls\">"
            )
            for url in urls:
                safe = html.escape(url, quote=True)
                parts.append(f'<li><a href="{safe}" rel="noreferrer">{safe}</a></li>')
            parts.append("</ul></section>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


# The downloads page is a fixed shell: the rows arrive as one push() per line
# from downloads-data.js, which is append-only. Sorting, filtering and paging
# all happen in the browser; the page itself never embeds the rows.
_DOWNLOADS_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Downloads</title>
<style>
:root{color-scheme:light dark}
body{font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:2rem auto;padding:0 1rem;color:#1d1d1f;background:#fff;max-width:1400px}
h1{font-size:1.5rem;margin:0 0 .25rem}
.meta{color:#666;margin:0 0 1rem}
.controls{display:flex;flex-wrap:wrap;gap:.6rem;align-items:end;margin:1rem 0}
.controls label{display:flex;flex-direction:column;font-size:.8rem;color:#555}
.controls input,.controls select,.controls button{font:inherit;padding:.3rem .45rem;border:1px solid #c9c9cf;border-radius:6px;background:transparent;color:inherit}
.controls input[type=search]{min-width:22rem}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #e4e4e8;vertical-align:top}
th{position:sticky;top:0;background:#fff;cursor:pointer;user-select:none;white-space:nowrap}
th.sorted.asc::after{content:" \\25B2"}th.sorted.desc::after{content:" \\25BC"}
td.num,th.num{text-align:right;white-space:nowrap}
td.url{word-break:break-all}
td.name{word-break:break-word}
a{color:#0645ad}
.pager{display:flex;gap:.6rem;align-items:center;margin:1rem 0;color:#555}
.pager button{font:inherit;padding:.3rem .7rem;border:1px solid #c9c9cf;border-radius:6px;background:transparent;color:inherit}
.pager button:disabled{opacity:.45}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}th{background:#111}th,td{border-color:#333}.controls input,.controls select,.controls button,.pager button{border-color:#555}a{color:#8ab4f8}}
</style></head><body>
<h1>Downloads</h1>
<p class="meta"><span id="summary">$TOTAL download(s)</span>; generated $STAMP</p>
<div class="controls">
  <input id="q" type="search" placeholder="Filter URL or filename..." aria-label="Filter URL or filename">
  <label>From <input id="from" type="date"></label>
  <label>To <input id="to" type="date"></label>
  <label>Rows <select id="size"><option>100</option><option selected>200</option><option>500</option><option>1000</option></select></label>
  <button id="reset" type="button">Reset</button>
</div>
<table id="t">
<thead><tr>
  <th data-key="date" class="sorted desc">Date</th>
  <th data-key="url">URL</th>
  <th data-key="name">Filename</th>
  <th data-key="bytes" class="num">Size</th>
</tr></thead>
<tbody></tbody>
</table>
<div class="pager"><button id="prev" type="button">Prev</button><span id="page"></span><button id="next" type="button">Next</button></div>
<script src="downloads-data.js?v=$VERSION"></script>
<script>
(function () {
  "use strict";
  const raw = window.DREDGE_DOWNLOADS || [];
  const byUrl = new Map();
  for (const row of raw) byUrl.set(row[1], row);
  const rows = Array.from(byUrl.values(), function (row) {
    const names = (row[2] || "").split(";").filter(Boolean);
    const sizes = (row[3] || "").split(";").filter(Boolean).map(Number);
    return {
      date: row[0] || "",
      url: row[1] || "",
      names: names,
      name: names.join("; "),
      bytes: sizes.reduce(function (a, b) { return a + (b || 0); }, 0)
    };
  });
  const q = document.getElementById("q");
  const from = document.getElementById("from");
  const to = document.getElementById("to");
  const size = document.getElementById("size");
  const tbody = document.querySelector("#t tbody");
  const summary = document.getElementById("summary");
  const pageLabel = document.getElementById("page");
  const prev = document.getElementById("prev");
  const next = document.getElementById("next");
  let view = rows;
  let page = 0;
  let perPage = Number(size.value);
  let sortKey = "date";
  let sortDir = -1;

  function fmt(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit === 0 ? value : value.toFixed(1)) + " " + units[unit];
  }
  function href(url) {
    return /^[a-z][a-z0-9+.-]*:\\/\\//i.test(url) ? url : "https://" + url;
  }
  function render() {
    const start = page * perPage;
    const end = Math.min(start + perPage, view.length);
    const fragment = document.createDocumentFragment();
    for (let i = start; i < end; i += 1) {
      const row = view[i];
      const tr = document.createElement("tr");
      const date = document.createElement("td");
      date.textContent = row.date;
      const url = document.createElement("td");
      url.className = "url";
      const link = document.createElement("a");
      link.href = href(row.url);
      link.textContent = row.url;
      link.rel = "noreferrer";
      link.target = "_blank";
      url.append(link);
      const name = document.createElement("td");
      name.className = "name";
      name.textContent = row.name;
      const bytes = document.createElement("td");
      bytes.className = "num";
      bytes.textContent = fmt(row.bytes);
      tr.append(date, url, name, bytes);
      fragment.append(tr);
    }
    tbody.replaceChildren(fragment);
    const pages = Math.max(1, Math.ceil(view.length / perPage));
    pageLabel.textContent = "Page " + (page + 1) + " / " + pages + " - " + view.length.toLocaleString() + " row(s)";
    prev.disabled = page <= 0;
    next.disabled = end >= view.length;
    summary.textContent = rows.length.toLocaleString() + " download(s)";
  }
  function apply(resetPage) {
    const needle = q.value.trim().toLowerCase();
    const start = from.value;
    const stop = to.value;
    view = rows.filter(function (row) {
      const day = row.date.slice(0, 10);
      if (start && day < start) return false;
      if (stop && day > stop) return false;
      if (needle && (row.url + "\\n" + row.name).toLowerCase().indexOf(needle) < 0) return false;
      return true;
    });
    view.sort(function (a, b) {
      if (sortKey === "bytes") return (a.bytes - b.bytes) * sortDir;
      const x = a[sortKey], y = b[sortKey];
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
    if (resetPage) page = 0;
    render();
  }
  let timer = 0;
  q.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { apply(true); }, 150);
  });
  from.addEventListener("change", function () { apply(true); });
  to.addEventListener("change", function () { apply(true); });
  size.addEventListener("change", function () {
    perPage = Number(size.value) || 200;
    apply(true);
  });
  document.getElementById("reset").addEventListener("click", function () {
    q.value = ""; from.value = ""; to.value = "";
    sortKey = "date"; sortDir = -1;
    document.querySelectorAll("th[data-key]").forEach(function (th) {
      th.classList.remove("sorted", "asc", "desc");
      if (th.dataset.key === "date") th.classList.add("sorted", "desc");
    });
    apply(true);
  });
  document.querySelectorAll("th[data-key]").forEach(function (th) {
    th.addEventListener("click", function () {
      const key = th.dataset.key;
      if (sortKey === key) sortDir = -sortDir;
      else { sortKey = key; sortDir = key === "date" ? -1 : 1; }
      document.querySelectorAll("th[data-key]").forEach(function (other) {
        other.classList.remove("sorted", "asc", "desc");
      });
      th.classList.add("sorted", sortDir === 1 ? "asc" : "desc");
      apply(false);
    });
  });
  prev.addEventListener("click", function () {
    if (page > 0) { page -= 1; render(); }
  });
  next.addEventListener("click", function () {
    if ((page + 1) * perPage < view.length) { page += 1; render(); }
  });
  apply(true);
})();
</script>
</body></html>
"""


def _download_digest(line: str) -> str:
    """Stable id for one archive row, so growth is detected without a rerender."""
    return hashlib.blake2b(line.encode("utf-8"), digest_size=12).hexdigest()


def _load_state(path: Path) -> set[str]:
    try:
        return set(path.read_text(encoding="utf-8", errors="replace").split())
    except OSError:
        return set()


def _write_downloads(root: Path, out: Path, *, rebuild: bool = False) -> Path:
    """Write the shell and append only rows not already in the data file.

    ``downloads.state`` holds one digest per exported row. A grown row (a post
    that gained a filename or a size) has a new digest, so it is pushed again;
    the page keeps the last push for a URL and therefore shows the grown row.
    A missing data or state file is treated as a request to rebuild, which is
    also what ``rebuild=True`` does.
    """
    data = out / DOWNLOAD_DATA
    state = out / DOWNLOAD_STATE
    if rebuild or not data.exists() or not state.exists():
        data.write_text("window.DREDGE_DOWNLOADS=[];\n", encoding="utf-8", newline="")
        state.write_text("", encoding="utf-8", newline="")
        seen: set[str] = set()
    else:
        seen = _load_state(state)

    total = 0
    with (
        data.open("a", encoding="utf-8", newline="") as data_fh,
        state.open("a", encoding="utf-8", newline="") as state_fh,
    ):
        for row in read_rows(root / "data" / "downloads.tsv"):
            line = format_row(row)
            digest = _download_digest(line)
            total += 1
            if digest in seen:
                continue
            seen.add(digest)
            payload = json.dumps(
                [
                    row.timestamp,
                    row.url,
                    ";".join(row.names),
                    ";".join(str(n) for n in row.sizes),
                ],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            data_fh.write(f"window.DREDGE_DOWNLOADS.push({payload});\n")
            state_fh.write(digest + "\n")

    now = datetime.now().astimezone()
    page = out / DOWNLOADS_PAGE
    page.write_text(
        _DOWNLOADS_SHELL.replace("$TOTAL", f"{total:,}").replace(
            "$STAMP", now.strftime("%Y-%m-%d %H:%M")
        ).replace("$VERSION", now.strftime("%Y%m%d%H%M%S")),
        encoding="utf-8",
        newline="",
    )
    return page


def open_reports(pages: Mapping[str, Path] | Iterable[Path]) -> None:
    """Open every generated page in the default browser."""
    paths = pages.values() if isinstance(pages, Mapping) else pages
    for path in paths:
        with contextlib.suppress(Exception):
            webbrowser.open(Path(path).resolve().as_uri())


def write_reports(
    root: str | Path,
    out: str | Path | None = None,
    *,
    rebuild: bool = False,
    open_pages: bool = False,
) -> dict[str, Path]:
    root = Path(root)
    target = Path(out) if out else root / "reports"
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, title in _LIST_PAGES:
        page = target / f"{name}.html"
        page.write_text(
            render(title, read_lines(root / "data" / f"{name}.txt")),
            encoding="utf-8",
            newline="",
        )
        written[name] = page
    written["downloads"] = _write_downloads(root, target, rebuild=rebuild)
    if open_pages:
        open_reports(written)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dredge-report",
        description="Render fail/skip/offline as HTML and downloads.tsv as a "
                    "sortable, filterable table.",
    )
    parser.add_argument("--root", default=None, help="dredge root (default: cwd)")
    parser.add_argument("--out", default=None, help="output folder (default: <root>/reports)")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="rewrite downloads-data.js from scratch instead of appending new rows",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="do not open the pages in the browser",
    )
    args = parser.parse_args(argv)

    written = write_reports(
        args.root or ".", args.out, rebuild=args.rebuild, open_pages=not args.no_open
    )
    for name, path in written.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
