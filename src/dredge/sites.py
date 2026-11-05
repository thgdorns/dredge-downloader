"""Parser for ``config/sites.txt`` and ``config/mirrors.txt``.

The files are the project's site knowledge: one line per host, keys documented
in the example file. Nothing in the code names a site. The parser is deliberately
tolerant: unknown keys are kept in ``extra`` so an older binary never rejects a
newer config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SIZE = re.compile(r"^(\d+)([KMG]?)$", re.IGNORECASE)

# Values that are comma-separated lists of hosts / options.
_LISTS = ("also", "probe", "cookiedrop")
# Values kept verbatim (case matters: gdl= options and URL templates).
_SCALARS = (
    "id", "tpl", "folder", "engine", "author", "postid", "posturl", "itemurl", "playlist",
    "strip", "dlstrip", "keepfrag", "list", "key", "dest", "rewrite",
)


@dataclass
class SiteRule:
    host: str = ""
    keep: int = 0                 # 0 = all
    min_size: int = 0             # bytes
    id: str = ""
    also: tuple[str, ...] = ()
    serial: bool = False
    native: bool = False
    probe: tuple[str, ...] = ()
    gdl: tuple[str, ...] = ()
    key: str = ""
    playlist: str = ""
    postid: str = ""
    posturl: str = ""
    itemurl: str = ""
    list: str = ""                # regex: this host's URLs yt-dlp can list flat
    tpl: str = ""
    folder: str = ""
    engine: str = ""
    author: str = ""              # "yt-dlp": creator folder from its uploader name
    dest: str = ""                # named destination for this host (dredge.conf)
    rewrite: str = ""             # REGEX=>URL applied to the URL before the engines
    cookiedrop: tuple[str, ...] = ()
    strip: str = ""
    dlstrip: str = ""
    keepfrag: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def matches(self, hostname: str) -> bool:
        h = hostname.lower().removeprefix("www.")
        if h == self.host or h.endswith("." + self.host):
            return True
        return any(h == a or h.endswith("." + a) for a in self.also)


def _size(value: str) -> int:
    m = _SIZE.match(value)
    if not m:
        return 0
    n = int(m.group(1))
    return n * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]


def _hosts(value: str) -> tuple[str, ...]:
    return tuple(v.strip().lower().removeprefix("www.") for v in value.split(",") if v.strip())


def parse_rule(line: str) -> SiteRule | None:
    """Parse one line; ``None`` for blank/comment lines."""
    text = line
    # a description starts at a " #" that follows whitespace, so a value may
    # itself contain a '#' (folder=[^/?#]+ is a real rule)
    cut = text.find(" #")
    if cut >= 0:
        text = text[:cut]
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split()
    host = parts[0].lower().removeprefix("www.")
    rule = SiteRule(host=host)
    for token in parts[1:]:
        if token == "serial":
            rule.serial = True
            continue
        if token == "native":
            rule.native = True
            continue
        key, sep, value = token.partition("=")
        if not sep:
            rule.extra[token] = ""
            continue
        if key == "keep":
            rule.keep = 0 if value == "all" else _size_or_int(value)
        elif key == "min":
            rule.min_size = _size(value)
        elif key in _LISTS:
            setattr(rule, key, _hosts(value) if key != "cookiedrop" else tuple(
                v.strip() for v in value.split(",") if v.strip()
            ))
        elif key == "gdl":
            rule.gdl = tuple(v.strip() for v in value.split(",") if v.strip())
        elif key in _SCALARS:
            setattr(rule, key, value.lower() if key in ("engine", "key", "dest", "author") else value)
        else:
            rule.extra[key] = value
    return rule


def _size_or_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def load_rules(path: str | Path) -> dict[str, SiteRule]:
    rules: dict[str, SiteRule] = {}
    p = Path(path)
    if not p.exists():
        return rules
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        rule = parse_rule(line)
        if rule and rule.host != "*":
            rules[rule.host] = rule
    return rules


def load_global_rule(path: str | Path) -> SiteRule:
    """The ``*`` line: strip/dlstrip/keepfrag rules that apply everywhere."""
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            rule = parse_rule(line)
            if rule and rule.host == "*":
                return rule
    return SiteRule(host="*")


def find_rule(rules: dict[str, SiteRule], hostname: str) -> SiteRule | None:
    """Most specific match wins (``ecchi.iwara.tv`` over ``iwara.tv``)."""
    best: SiteRule | None = None
    best_len = -1
    for key, rule in rules.items():
        if rule.matches(hostname) and len(key) > best_len:
            best, best_len = rule, len(key)
    return best


@dataclass(frozen=True)
class MirrorTable:
    """Host substitutions from ``config/mirrors.txt``.

    ``mirrors``: fetch the same path from a working host (kemono -> pawchive).
    ``aliases``: the same site under another name; also the same download.
    ``lang``:    bare domains whose two-letter subdomains are the same site.
    """

    mirrors: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    lang: tuple[str, ...] = ()

    def lang_host(self, hostname: str) -> str:
        h = hostname.lower()
        bits = h.split(".")
        if len(bits) < 3 or len(bits[0]) != 2:
            return h
        parent = ".".join(bits[1:])
        return parent if parent in self.lang else h


def load_mirrors(path: str | Path) -> MirrorTable:
    mirrors: dict[str, str] = {}
    aliases: dict[str, str] = {}
    lang: list[str] = []
    p = Path(path)
    if not p.exists():
        return MirrorTable()
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split()
        if len(parts) < 2:
            continue
        first = parts[0].lower()
        if first == "lang":
            lang.append(parts[1].lower().removeprefix("www."))
        elif first == "alias" and len(parts) >= 3:
            aliases[parts[1].lower().removeprefix("www.")] = parts[2].lower().removeprefix("www.")
        else:
            mirrors[first.removeprefix("www.")] = parts[1].lower().removeprefix("www.")
    return MirrorTable(mirrors=mirrors, aliases=aliases, lang=tuple(lang))
