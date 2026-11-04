from dredge.urls import canon, host, seen_id


def test_host_strips_www_and_scheme():
    assert host("https://www.Example.com/a") == "example.com"
    assert host("example.com/a") == "example.com"


def test_canon_drops_tracking_and_trailing_slash():
    assert canon("https://www.example.com/Path/?utm_source=x&v=1") == "example.com/Path?v=1"


def test_canon_keeps_path_case():
    assert canon("https://example.com/Video/AbC") == "example.com/Video/AbC"


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
