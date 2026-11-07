import urllib.error

from dredge.urls import (
    canon,
    host,
    is_shortener,
    resolve_short_url,
    seen_id,
)


def test_host_strips_www_and_scheme():
    assert host("https://www.Example.com/a") == "example.com"
    assert host("example.com/a") == "example.com"


def test_canon_drops_tracking_and_trailing_slash():
    assert canon("https://www.example.com/Path/?utm_source=x&v=1") == "example.com/Path?v=1"


def test_canon_keeps_path_case():
    assert canon("https://example.com/Video/AbC") == "example.com/Video/AbC"


def test_canon_keeps_the_viewer_page_from_the_fragment():
    """#ppixiv?page=5 selects one image, so pages are separate downloads."""
    assert canon("https://www.pixiv.net/artworks/151761889#ppixiv?page=5") == (
        "pixiv.net/artworks/151761889?page=5"
    )
    assert canon("https://www.pixiv.net/artworks/151761889#ppixiv?page=0") == (
        "pixiv.net/artworks/151761889?page=0"
    )
    assert canon("https://www.pixiv.net/artworks/151761889#ppixiv") == (
        "pixiv.net/artworks/151761889"
    )
    assert canon("https://www.pixiv.net/artworks/151761889") == (
        "pixiv.net/artworks/151761889"
    )


def test_canon_keeps_identity_fragments():
    assert canon("https://example.com/x#file=123") == "example.com/x#file=123"
    assert canon("https://example.com/x#comments") == "example.com/x"


def test_seen_id_matches_id_alone_or_after_slug():
    assert seen_id("iwara.tv/video/AbCdEf123456", "[A-Za-z0-9]{10,20}") == "abcdef123456"
    assert seen_id("iwara.tv/video/AbCdEf123456/some-slug", "[A-Za-z0-9]{10,20}") == "abcdef123456"
    assert seen_id("example.com/x", "[A-Za-z0-9]{10,20}") == ""


def test_host_strips_port_and_userinfo():
    assert host("http://127.0.0.1:8766/x") == "127.0.0.1"
    assert host("https://user:pass@www.example.com:443/a") == "example.com"


class _Response:
    def __init__(self, url: str) -> None:
        self.url = url

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_shortener_hosts_match_subdomains():
    assert is_shortener("https://goo.gl/jkGHi0", ("goo.gl",))
    assert is_shortener("https://www.goo.gl/x", ("goo.gl",))
    assert not is_shortener("https://example.com/x", ("goo.gl",))


def test_resolve_short_url_follows_the_redirect():
    def opener(request, timeout):
        assert request.get_method() == "HEAD"
        return _Response("https://drive.google.com/drive/folders/abc")

    assert resolve_short_url(
        "https://goo.gl/jkGHi0", ("goo.gl",), opener=opener
    ) == "https://drive.google.com/drive/folders/abc"


def test_resolve_short_url_falls_back_to_get():
    methods = []

    def opener(request, timeout):
        methods.append(request.get_method())
        if request.get_method() == "HEAD":
            raise urllib.error.URLError("HEAD refused")
        return _Response("https://example.com/final")

    assert resolve_short_url(
        "https://bit.ly/x", ("bit.ly",), opener=opener
    ) == "https://example.com/final"
    assert methods == ["HEAD", "GET"]


def test_resolve_short_url_falls_back_to_plain_get():
    methods = []

    def opener(request, timeout):
        methods.append((request.get_method(), request.get_header("Range")))
        if request.get_method() == "HEAD" or request.get_header("Range"):
            raise urllib.error.URLError("refused")
        return _Response("https://example.com/final")

    assert resolve_short_url(
        "https://bit.ly/x", ("bit.ly",), opener=opener
    ) == "https://example.com/final"
    assert methods == [("HEAD", None), ("GET", "bytes=0-0"), ("GET", None)]


def test_resolve_short_url_rejects_a_non_http_target():
    def opener(request, timeout):
        return _Response("ftp://example.com/file")

    assert resolve_short_url(
        "https://goo.gl/x", ("goo.gl",), opener=opener
    ) == "https://goo.gl/x"


def test_resolve_short_url_keeps_a_final_4xx_target():
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            "https://drive.google.com/final", 404, "Not Found", {}, None
        )

    assert resolve_short_url(
        "https://goo.gl/x", ("goo.gl",), opener=opener
    ) == "https://drive.google.com/final"


def test_resolve_short_url_returns_the_original_on_failure():
    def opener(request, timeout):
        raise urllib.error.URLError("offline")

    assert resolve_short_url("https://goo.gl/x", ("goo.gl",), opener=opener) == (
        "https://goo.gl/x"
    )


def test_resolve_short_url_ignores_unlisted_hosts():
    def opener(request, timeout):
        raise AssertionError("should not be called")

    assert resolve_short_url(
        "https://example.com/x", ("goo.gl",), opener=opener
    ) == "https://example.com/x"
