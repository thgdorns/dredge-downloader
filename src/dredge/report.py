"""Render ``fail.txt`` / ``offline.txt`` as HTML, grouped by domain.

One file per record; domains are listed alphabetically, URLs inside a domain
too, duplicates dropped. Each page is self-contained (no external CSS or JS).
"""

from __future__ import annotations

import argparse
import html
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .records import read_lines


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
        url = raw.strip()
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


def write_reports(root: str | Path, out: str | Path | None = None) -> dict[str, Path]:
    root = Path(root)
    target = Path(out) if out else root / "reports"
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, title in (("fail", "Failed links"), ("offline", "Offline (dead) links")):
        source = root / "data" / f"{name}.txt"
        page = target / f"{name}.html"
        page.write_text(render(title, read_lines(source)), encoding="utf-8")
        written[name] = page
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dredge-report",
        description="Render fail.txt and offline.txt as HTML grouped by domain.",
    )
    parser.add_argument("--root", default=None, help="dredge root (default: cwd)")
    parser.add_argument("--out", default=None, help="output folder (default: <root>/reports)")
    args = parser.parse_args(argv)

    written = write_reports(args.root or ".", args.out)
    for name, path in written.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
