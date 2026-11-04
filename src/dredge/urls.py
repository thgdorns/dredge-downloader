"""URL canonicalisation and identity helpers.

Ported from the PowerShell implementation. Pure functions plus a ``Knowledge``
object that carries the ``sites.txt`` / ``mirrors.txt`` rules; nothing here
names a site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .sites import MirrorTable, SiteRule, load_global_rule, load_mirrors, load_rules

# Query arguments that never identify an item. ``sites.txt`` overrides this with
# its ``*`` line; the default is the legacy value so a missing config still
# canonicalises the same way.
DEFAULT_STRIP = re.compile(
    r"^(utm_.*|gclid|fbclid|igshid|msclkid|ad_sub|subid\d*|campaign_id|campaign|"
    r"reff|ref_src|site_id|trx|promo|aff|usp|is_from_webapp|feature|ref|ch|t|"
    r"time_continue|timestamp)$",
    re.IGNORECASE,
)
DEFAULT_DLSTRIP = re.compile(
    r"^(utm_.*|gclid|fbclid|igshid|msclkid|ad_sub|subid\d*|campaign_id|campaign|"
    r"reff|ref_src|site_id|trx|promo|aff)$",
    re.IGNORECASE,
)
DEFAULT_KEEPFRAG = re.compile(r"^(file|item)=|/file/|^!", re.IGNORECASE)

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_EXT_TAIL = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _strip_scheme(url: str) -> str:
    return _HTTP.sub("", url.strip())


def host(url: str) -> str:
    """Lowercase host without port, userinfo or a leading ``www.``; ``""`` if unparsable."""
    u = url.strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    try:
        name = urlsplit(u).hostname or ""
    except ValueError:
        return ""
    return name.lower().removeprefix("www.")


def _bare(hostname: str, mirrors: MirrorTable | None) -> str:
    h = hostname.lower().removeprefix("www.")
    if mirrors:
        h = mirrors.lang_host(h)
        h = mirrors.aliases.get(h, h)
    return h


def _has_token(rest: str) -> bool:
    for seg in rest.split("/"):
        if not seg:
            continue
        core = _EXT_TAIL.sub("", seg)
        alnum = re.sub(r"[^0-9A-Za-z]", "", core)
        if len(alnum) >= 8 and re.search(r"[0-9]", alnum):
            return True
    return False


def _query_key(param: str) -> str:
    return param.split("=", 1)[0].lower()


def canon(
    url: str,
    *,
    strip: re.Pattern[str] | None = None,
    keepfrag: re.Pattern[str] | None = None,
    key_param: str = "",
    mirrors: MirrorTable | None = None,
) -> str:
    """Canonical form stored in the archive and used for duplicate checks.

    Removes the scheme, a leading ``www.``, a trailing slash and tracking
    arguments. The path keeps its case: many sites use case-sensitive ids.
    A path that carries an identity token (8+ alphanumerics including a digit)
    drops the query entirely; otherwise the query is kept minus tracking
    arguments, or only the site's ``key=`` argument when it has one.
    """
    if not url:
        return ""
    strip_re = strip or DEFAULT_STRIP
    keep_re = keepfrag or DEFAULT_KEEPFRAG
    u = _strip_scheme(url)

    # fragment first, then query, then host/path: a pathless URL
    # (gofile.io/?c=..., mega.nz/#!KEY) would otherwise glue the query or
    # fragment onto the host and get lowercased on the next pass.
    frag = ""
    fi = u.find("#")
    if fi >= 0:
        frag = u[fi + 1:]
        u = u[:fi]
    keep_fragment = bool(frag and keep_re.search(frag))

    query = ""
    qi = u.find("?")
    if qi >= 0:
        query = u[qi + 1:]
        u = u[:qi]

    slash = u.find("/")
    if slash < 0:
        slash = len(u)
    h = _bare(u[:slash], mirrors)
    rest = u[slash:]

    if _has_token(rest):
        query = ""
    elif query:
        has_key = bool(key_param) and any(
            _query_key(kv) == key_param for kv in query.split("&") if kv
        )
        keep: list[str] = []
        for kv in query.split("&"):
            if not kv:
                continue
            k = _query_key(kv)
            if strip_re.search(k):
                continue
            if has_key and k != key_param:
                continue
            keep.append(kv)
        query = "?" + "&".join(keep) if keep else ""

    if len(rest) > 1 and rest.endswith("/"):
        rest = rest[:-1]
    if rest == "/":
        rest = ""

    out = h + rest + query
    if keep_fragment:
        out += "#" + frag
    return out


@lru_cache(maxsize=512)
def _id_pattern(rule_id: str) -> re.Pattern[str]:
    return re.compile(r"^(?:.*-)?(" + rule_id + r")$")


def seen_id(url: str, rule_id: str = "") -> str:
    """The site's item id extracted with a ``sites.txt`` rule (bare id).

    ``rule_id`` is a regex matched against one path segment, alone or trailing a
    slug (``/videos/<slug>-<id>``). Returns ``""`` when the rule does not match.
    """
    if not rule_id:
        return ""
    u = url.strip()
    if not _SCHEME.match(u):
        u = "http://" + u
    path = re.split(r"[?#]", u, maxsplit=1)[0]
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", path, flags=re.IGNORECASE)
    pattern = _id_pattern(rule_id)
    for segment in path.strip("/").split("/"):
        m = pattern.match(segment)
        if m:
            return m.group(1).lower()
    return ""


def dl_url(url: str, *, strip: re.Pattern[str] | None = None,
           mirrors: MirrorTable | None = None) -> str:
    """The URL actually handed to the engines.

    Same tracking-parameter idea as :func:`canon`, but the URL keeps its original
    shape (scheme, host case, query order) because it is used for downloading.
    Mirror and alias substitution happens first.
    """
    if not url:
        return ""
    u = url
    hs = u.find("://")
    if hs >= 0 and mirrors:
        after = u[hs + 3:]
        slash = after.find("/")
        host_part = after if slash < 0 else after[:slash]
        raw = host_part.lower().removeprefix("www.")
        orig = raw
        raw = mirrors.lang_host(raw)
        bare = mirrors.aliases.get(raw, raw)
        bare = mirrors.mirrors.get(bare, bare)
        if bare != orig:
            u = u[: hs + 3] + bare + ("" if slash < 0 else after[slash:])

    qi = u.find("?")
    if qi < 0:
        return u
    fi = u.find("#", qi)
    frag = ""
    if fi >= 0:
        frag = u[fi:]
        u = u[:fi]
    base = u[:qi]
    query = u[qi + 1:]
    strip_re = strip or DEFAULT_DLSTRIP
    keep = [kv for kv in query.split("&") if kv and not strip_re.search(_query_key(kv))]
    return base + ("?" + "&".join(keep) if keep else "") + frag


@dataclass(frozen=True)
class Playlist:
    host: str
    service: str
    user: str
    rule: SiteRule


@dataclass(frozen=True)
class Knowledge:
    """Every site rule the run needs, loaded once."""

    rules: dict[str, SiteRule]
    global_rule: SiteRule
    mirrors: MirrorTable
    # host -> rule, including `also=` aliases; built in __post_init__
    _index: dict[str, SiteRule] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[str, SiteRule] = {}
        for key, rule in self.rules.items():
            index.setdefault(key, rule)
            for alias in rule.also:
                index.setdefault(alias, rule)
        object.__setattr__(self, "_index", index)

    @classmethod
    def load(cls, config_dir: str | Path) -> Knowledge:
        config = Path(config_dir)
        return cls(
            rules=load_rules(config / "sites.txt"),
            global_rule=load_global_rule(config / "sites.txt"),
            mirrors=load_mirrors(config / "mirrors.txt"),
        )

    @property
    def strip(self) -> re.Pattern[str]:
        if self.global_rule.strip:
            try:
                return re.compile(self.global_rule.strip, re.IGNORECASE)
            except re.error:
                pass
        return DEFAULT_STRIP

    @property
    def dlstrip(self) -> re.Pattern[str]:
        if self.global_rule.dlstrip:
            try:
                return re.compile(self.global_rule.dlstrip, re.IGNORECASE)
            except re.error:
                pass
        return DEFAULT_DLSTRIP

    @property
    def keepfrag(self) -> re.Pattern[str]:
        if self.global_rule.keepfrag:
            try:
                return re.compile(self.global_rule.keepfrag, re.IGNORECASE)
            except re.error:
                pass
        return DEFAULT_KEEPFRAG

    def rule(self, url: str) -> SiteRule | None:
        """Rule for a URL (or a bare host); subdomains match, most specific wins."""
        h = host(url)
        if not h:
            return None
        return self.rule_for_host(h)

    def rule_for_host(self, hostname: str) -> SiteRule | None:
        """Rule for a host: walk the labels from the full host upwards."""
        if not hostname:
            return None
        parts = hostname.lower().removeprefix("www.").split(".")
        for index in range(len(parts)):
            rule = self._index.get(".".join(parts[index:]))
            if rule is not None:
                return rule
        return None

    def canon(self, url: str) -> str:
        rule = self.rule(url)
        return canon(
            url,
            strip=self.strip,
            keepfrag=self.keepfrag,
            key_param=rule.key if rule else "",
            mirrors=self.mirrors,
        )

    def seen_id(self, url: str) -> str:
        """Auxiliary archive key (``id:<host>:<id>``) or ``""``."""
        rule = self.rule(url)
        if not rule or not rule.id:
            return ""
        value = seen_id(url, rule.id)
        return f"id:{rule.key or rule.host}:{value}" if value else ""

    def dl_url(self, url: str) -> str:
        return dl_url(url, strip=self.dlstrip, mirrors=self.mirrors)

    def playlist(self, url: str) -> Playlist | None:
        """A creator/user page: one URL that expands into many posts."""
        if not url:
            return None
        u = url if _SCHEME.match(url) else "http://" + url
        rule = self.rule(u)
        if not rule or not rule.playlist or not rule.postid or not rule.posturl:
            return None
        m = re.match(
            r"(?i)^(?:https?://)?(?:www\.)?([^/]+\.[^/]+)/([^/]+)/"
            + re.escape(rule.playlist)
            + r"/([^/?#]+)/?(?:[?#].*)?$",
            u.strip(),
        )
        if not m:
            return None
        return Playlist(
            host=m.group(1).lower(),
            service=m.group(2).lower(),
            user=unquote(m.group(3)),
            rule=rule,
        )
