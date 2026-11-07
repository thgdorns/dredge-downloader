"""URL canonicalisation and identity helpers.

Ported from the PowerShell implementation. Pure functions plus a ``Knowledge``
object that carries the ``sites.txt`` / ``mirrors.txt`` rules; nothing here
names a site.
"""

from __future__ import annotations

import http.cookiejar
import re
import urllib.error
import urllib.request
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


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def is_shortener(url: str, hosts: tuple[str, ...]) -> bool:
    """True when the URL's host is in the configured shortener list."""
    h = host(url)
    return bool(h) and any(h == d or h.endswith("." + d) for d in hosts)


def resolve_short_url(
    url: str,
    hosts: tuple[str, ...],
    *,
    timeout: float = 10.0,
    opener=None,
) -> str:
    """Follow a shortener's HTTP redirects and return the final URL.

    Only hosts in ``hosts`` are touched, so a normal link costs nothing. HEAD is
    tried first (no body); some servers refuse it, so a ranged GET is the
    fallback. Any failure returns the original URL, leaving the normal tier
    chain to decide what to do.
    """
    if not hosts or not is_shortener(url, hosts):
        return url
    if not url.lower().startswith(("http://", "https://")):
        return url
    if opener is None:
        # A cookie jar matches requests.Session: some shorteners set a consent
        # cookie on the first hop and expect it back on the redirect.
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        ).open
    open_url = opener
    headers = {"User-Agent": _BROWSER_UA, "Accept": "*/*"}
    attempts = (("HEAD", {}), ("GET", {"Range": "bytes=0-0"}), ("GET", {}))
    for method, extra in attempts:
        request = urllib.request.Request(
            url, headers={**headers, **extra}, method=method
        )
        try:
            with open_url(request, timeout=timeout) as response:
                final = response.geturl()
        except urllib.error.HTTPError as exc:
            # requests does not raise on 4xx: the final URL is still the answer,
            # so the engine can classify the target instead of the shortener.
            final = exc.geturl()
            if final and final != url and final.lower().startswith(
                ("http://", "https://")
            ):
                return final
            continue
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if final and final != url and final.lower().startswith(("http://", "https://")):
            return final
    return url


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
           mirrors: MirrorTable | None = None, rewrite: str = "") -> str:
    """The URL actually handed to the engines.

    Same tracking-parameter idea as :func:`canon`, but the URL keeps its original
    shape (scheme, host case, query order) because it is used for downloading.
    Mirror and alias substitution happens first.
    """
    if not url:
        return ""
    u = url
    if rewrite and "=>" in rewrite:
        pattern, _, replacement = rewrite.partition("=>")
        try:
            u = re.sub(pattern, replacement, u, count=1)
        except re.error:
            pass
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

    def keys(self, url: str) -> tuple[str, ...]:
        """Canonical duplicate keys: the URL and the host it is mirrored to.

        A plain ``mirrors.txt`` entry rewrites the URL for downloading but keeps
        the queued shape in the archive. Adding the mirror target as a second
        key makes the kemono/pawchive twins the same download without changing
        what the TSV row shows. This works on scheme-less archive keys too.
        """
        canonical = self.canon(url)
        if not canonical:
            return ()
        hostname, sep, rest = canonical.partition("/")
        target = self.mirrors.mirrors.get(hostname, hostname)
        mirrored = target + (sep + rest if sep else "")
        return tuple(dict.fromkeys(k for k in (canonical, mirrored) if k))

    def dl_url(self, url: str) -> str:
        rule = self.rule(url)
        return dl_url(
            url, strip=self.dlstrip, mirrors=self.mirrors,
            rewrite=rule.rewrite if rule else "",
        )

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
