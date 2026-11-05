"""Probe mode: turn a copied page into queue lines, without downloading.

Ported from the PowerShell implementation. A playlist, channel or creator page
is listed by whichever engine can do it cheapest:

1. ``list=`` in ``sites.txt`` and yt-dlp available -> a flat listing (seconds);
2. ``--probe-files`` and gallery-dl available -> ``-g``, one line per file;
3. gallery-dl otherwise -> ``--dump-json``; the item URL is built from the
   site's ``itemurl=``/``posturl=`` template;
4. yt-dlp as a last resort, flat first, then a normal simulated extraction.

Nothing is downloaded, and nothing is queued before the list is classified:
items already in the queue, skip.txt, fail.txt/offline.txt - or in the archive
when the records check is on - are counted and left alone. This applies to
post-level items; ``-g`` file lists are not filtered by gallery-dl's archive
(see :func:`gdl_file_urls`).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import paint, records, state
from .clipboard import ClipboardWatcher, available
from .config import Paths, Runtime
from .engines import Engines, run
from .sites import SiteRule
from .urls import Knowledge
from .urls import host as url_host

Runner = Callable[[Sequence[str], str | Path], tuple[int, str]]

_HTTP = re.compile(r"(?i)^https?://")


@dataclass(frozen=True)
class Item:
    url: str
    title: str = ""
    why: str = ""          # "", queued, skip.txt, failed/offline, downloaded


@dataclass
class Result:
    url: str
    source: str = ""
    items: list[Item] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def new(self) -> list[Item]:
        return [item for item in self.items if not item.why]

    def summary(self) -> str:
        return (
            f"{self.counts.get('new', 0)} new -> queue.txt; "
            f"{self.counts.get('queued', 0)} already queued; "
            f"{self.counts.get('skip', 0)} in skip.txt; "
            f"{self.counts.get('failed', 0)} known-failed/offline; "
            f"{self.counts.get('downloaded', 0)} already downloaded; "
            f"{self.counts.get('unusable', 0)} unusable"
        )


def _default_runner(args: Sequence[str], cwd: str | Path) -> tuple[int, str]:
    return run(args, cwd=cwd)


def _url_path(url: str) -> str:
    match = re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*://)?[^/?#]+([^?#]*)", url)
    return match.group(1) if match else ""


def _temp_archive() -> Path:
    return Path(tempfile.gettempdir()) / f"probe-archive-{time.time_ns()}.txt"


def _gdl_base(paths: Paths) -> list[str]:
    args = ["--config-ignore", "-c", str(paths.gallery_conf)]
    if paths.gallery_runtime.exists():
        args += ["-c", str(paths.gallery_runtime)]
    return args


def fill_template(template: str, data: dict[str, str]) -> str:
    """Fill ``{host} {service} {user} {id} {slug}``; empty when a key is missing."""
    out = template
    for key, value in data.items():
        out = out.replace("{" + key + "}", value or "")
    return "" if "{" in out else out


def _http_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        url = line.strip()
        if _HTTP.match(url):
            out.append(url)
    return out


def yt_items(url: str, *, paths: Paths, engines: Engines, runner: Runner,
             flat: bool) -> list[Item]:
    """Item URLs from yt-dlp, with whatever title it printed."""
    args = [
        engines.yt or "yt-dlp", "--ignore-config",
        "--config-locations", str(paths.ytdlp_conf), "--no-download-archive",
        "--simulate", "--print", "%(webpage_url)s\t%(title)s",
    ]
    if flat:
        args.append("--flat-playlist")
    args.append(url)
    _rc, text = runner(args, paths.root)
    items: list[Item] = []
    for line in text.splitlines():
        parts = line.strip().split("\t", 1)
        if not _HTTP.match(parts[0]):
            continue
        title = parts[1].strip() if len(parts) > 1 else ""
        items.append(Item(parts[0].strip(), title))
    return items


def gdl_file_urls(url: str, *, paths: Paths, engines: Engines,
                  runner: Runner) -> list[Item]:
    """One item per downloadable file URL (``gallery-dl -g``).

    ``-g`` deliberately ignores gallery-dl's download archive, so this is a
    full file list for review, not a new-only list. A file is only classified
    as downloaded when that exact file URL is in Dredge's archive (for example
    after a file-level probe was itself downloaded). Post-level probe and the
    engine archive handle post deduplication.
    """
    archive = _temp_archive()
    args = [
        engines.gdl or "gallery-dl", *_gdl_base(paths),
        "--simulate", "-g", "--download-archive", str(archive), url,
    ]
    try:
        _rc, text = runner(args, paths.root)
    finally:
        try:
            archive.unlink()
        except OSError:
            pass
    return [Item(line) for line in _http_lines(text)]


def parse_dump(text: str) -> list:
    """Parse ``gallery-dl --dump-json`` output, ignoring warning lines before it.

    ``run()`` merges stderr into stdout, so a ``[gallery-dl][warning] ...`` line
    can precede the JSON array. The array itself starts on its own line (``[``),
    which is not true of those warnings.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "[":
            try:
                return json.loads("\n".join(lines[index:]))
            except ValueError:
                return []
    start = text.find("[[")          # compact output: one line, no warnings
    if start < 0:
        return []
    try:
        return json.loads(text[start:])
    except ValueError:
        return []


def gdl_posts(url: str, *, paths: Paths, engines: Engines, runner: Runner,
              rule: SiteRule | None) -> list[Item]:
    """Post items from ``--dump-json``, with URLs built from the site template."""
    archive = _temp_archive()
    args = [
        engines.gdl or "gallery-dl", *_gdl_base(paths),
        "--simulate", "--dump-json", "--download-archive", str(archive), url,
    ]
    try:
        _rc, text = runner(args, paths.root)
    finally:
        try:
            archive.unlink()
        except OSError:
            pass

    payload = parse_dump(text)
    posts = [
        entry[1] for entry in payload
        if isinstance(entry, list) and len(entry) >= 2
        and entry[0] == 2 and isinstance(entry[1], dict)
    ]
    template = ""
    if rule:
        template = rule.itemurl or rule.posturl

    items: list[Item] = []
    for data in posts:
        item_url = ""
        if template:
            item_url = fill_template(template, {
                "host": url_host(url),
                "service": str(data.get("service", "")),
                "user": str(data.get("user", "")),
                "id": str(data.get("id", "")),
                "slug": str(data.get("slug", "")),
            })
        if not item_url and len(posts) == 1:
            item_url = url
        if not _HTTP.match(item_url):
            continue
        items.append(Item(item_url, str(data.get("title", ""))))
    return items


def _has(keyset: set[str], url: str, keys: tuple[str, ...], ident: str) -> bool:
    return (
        url in keyset
        or any(key in keyset for key in keys)
        or (ident != "" and ident in keyset)
    )


def classify(
    items: Iterable[Item],
    *,
    knowledge: Knowledge,
    seen: set[str],
    queued: set[str],
    skip: set[str],
    failed: set[str],
    archive_check: bool = True,
) -> tuple[list[Item], dict[str, int]]:
    """Attach a reason to every item; counts by reason."""
    counts = {"new": 0, "queued": 0, "skip": 0, "failed": 0, "downloaded": 0,
              "unusable": 0, "duplicate": 0}
    unique: set[str] = set()
    out: list[Item] = []
    for item in items:
        keys = knowledge.keys(item.url)
        if not keys:
            counts["unusable"] += 1
            continue
        if any(key in unique for key in keys):
            counts["duplicate"] += 1
            continue
        unique.update(keys)
        ident = knowledge.seen_id(keys[0])

        why = ""
        if _has(queued, item.url, keys, ident):
            why = "queued"
        elif archive_check and _has(skip, item.url, keys, ident):
            why = "skip.txt"
        elif archive_check and _has(failed, item.url, keys, ident):
            why = "failed/offline"
        elif archive_check and _has(seen, item.url, keys, ident):
            why = "downloaded"
        counts[{"": "new", "queued": "queued", "skip.txt": "skip",
                "failed/offline": "failed", "downloaded": "downloaded"}[why]] += 1
        out.append(Item(item.url, item.title, why))
    return out, counts


def probe(
    url: str,
    *,
    files: bool,
    knowledge: Knowledge,
    paths: Paths,
    engines: Engines,
    seen: set[str] | None = None,
    queued: set[str] | None = None,
    skip: set[str] | None = None,
    failed: set[str] | None = None,
    archive_check: bool = True,
    runner: Runner = _default_runner,
) -> Result:
    """List a page and classify what is already known. No downloads, no writes."""
    url = url.strip()
    result = Result(url)
    if not _HTTP.match(url):
        result.counts = {"unusable": 1}
        return result

    rule = knowledge.rule(url)
    if rule and rule.engine and rule.engine != "gdl":
        result.counts = {"filehost": 1}
        return result

    items: list[Item] = []
    source = ""

    if not files and rule and rule.list and engines.yt:
        path = _url_path(url)
        if path and re.search(rule.list, path):
            items = yt_items(url, paths=paths, engines=engines, runner=runner, flat=True)
            if items:
                source = "yt-dlp flat"

    if engines.gdl and files:
        items = gdl_file_urls(url, paths=paths, engines=engines, runner=runner)
        if items:
            source = "gallery-dl -g"

    if engines.gdl and not files and not items:
        items = gdl_posts(url, paths=paths, engines=engines, runner=runner, rule=rule)
        if items:
            source = "gallery-dl"

    if not items and engines.yt:
        for flat in (True, False):
            items = yt_items(url, paths=paths, engines=engines, runner=runner, flat=flat)
            if items:
                source = "yt-dlp"
                break

    result.source = source
    result.items, result.counts = classify(
        items,
        knowledge=knowledge,
        seen=seen or set(),
        queued=queued or set(),
        skip=skip or set(),
        failed=failed or set(),
        archive_check=archive_check,
    )
    return result


def format_result(result: Result) -> str:
    lines = [
        f"probe {result.url}  ({result.source or 'nothing listed'})",
        result.summary(),
        "",
    ]
    for item in result.items:
        mark = f"  [{item.why}]" if item.why else "  [new]"
        title = f"  {item.title}" if item.title else ""
        lines.append(item.url + mark + title)
    return "\n".join(lines)


# -- interactive mode --------------------------------------------------------

def _escape_pressed() -> bool:
    """True when Esc was pressed since the last check (Windows)."""
    if sys.platform != "win32":
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        while msvcrt.kbhit():
            if msvcrt.getwch() == "\x1b":
                return True
    except OSError:
        return False
    return False


def run_mode(
    runtime: Runtime,
    knowledge: Knowledge,
    engines: Engines,
    *,
    files: bool = False,
    out=print,
) -> int:
    """Watch the clipboard (or drain the inbox once) until Esc/Ctrl+C."""
    paths = runtime.paths
    paths.ensure()
    was_paused = paths.paused.exists()
    paths.paused.touch()

    interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive and not available():
        out(paint.yellow("    clipboard backend not installed (pyperclip) - draining the inbox once."))
        interactive = False
    mode = "file" if files else "post"
    out(paint.cyan(
        f"    probe mode ({mode} level): copy a playlist/user-page link; its new items go to queue.txt."
    ))
    out(paint.darkgray(
        "    downloads stay paused until you end probe mode (Esc or Ctrl+C), which resumes the queue."
    ))

    state.update_archive_sets(runtime, knowledge)
    state.update_failed_set(runtime, knowledge)
    queued = records.keyed_set(records.read_lines(paths.queue), knowledge)
    watcher = ClipboardWatcher()
    handled: set[str] = set()

    def handle(url: str) -> None:
        if url in handled:
            return
        handled.add(url)
        result = probe(
            url, files=files, knowledge=knowledge, paths=paths, engines=engines,
            seen=runtime.seen, queued=queued, skip=runtime.skip_always,
            failed=runtime.failed, archive_check=runtime.archive_check,
        )
        if not result.items and result.counts.get("filehost"):
            out(paint.yellow(f"    {url_host(url)} is a file host: the link has no per-file URLs."))
            return
        report = format_result(result)
        for position, line in enumerate(report.splitlines()):
            # the summary line under the probe heading was Gray in the legacy menu
            out(paint.gray(line) if position == 1 else line)
        if result.new:
            records.append_lines(paths.queue, [item.url for item in result.new])
            for item in result.new:
                queued.update(knowledge.keys(item.url))
            out(paint.yellow(
                "    downloads are PAUSED: review queue.txt, then resume with menu 1 -> 6."
            ))
        log = paths.logs / time.strftime("probe-%Y%m%d-%H%M%S.txt")
        try:
            log.write_text(report + "\n", encoding="utf-8")
        except OSError:
            pass
        out(paint.green(
            f"    {len(result.items)} link(s) found ({result.source or 'nothing'}); list saved to {log}"
        ))

    try:
        if not interactive:
            for url in state.read_clip_inbox(paths.clip_inbox):
                handle(url)
            return 0
        while True:
            batch = watcher.poll() + state.read_clip_inbox(paths.clip_inbox)
            for url in batch:
                handle(url)
            if _escape_pressed():
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if not was_paused:
            try:
                paths.paused.unlink()
            except OSError:
                pass
            out(paint.green("    probe mode ended - downloads resumed."))
        else:
            out(paint.yellow("    probe mode ended - downloads were already paused and stay paused."))
    return 0
