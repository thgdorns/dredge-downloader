from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge, dl_url


def build(rules: dict[str, SiteRule] | None = None, mirrors: MirrorTable | None = None,
          global_rule: SiteRule | None = None) -> Knowledge:
    return Knowledge(
        rules=rules or {},
        global_rule=global_rule or SiteRule(host="*"),
        mirrors=mirrors or MirrorTable(),
    )


def test_key_parameter_keeps_only_the_item_argument():
    knowledge = build({"example.com": parse_rule("example.com key=v")})
    assert knowledge.canon("https://example.com/watch?x=1&v=abc&utm_source=y") == (
        "example.com/watch?v=abc"
    )


def test_canonical_form_survives_two_url_shapes_of_one_item():
    knowledge = build({"example.com": parse_rule("example.com id=[0-9]{6,}")})
    a = knowledge.canon("https://example.com/post/1234567?utm_source=x")
    b = knowledge.canon("http://www.example.com/post/1234567/")
    assert a == b == "example.com/post/1234567"


def test_seen_id_is_scoped_to_the_host():
    knowledge = build({"example.com": parse_rule("example.com id=[0-9]{6,}")})
    assert knowledge.seen_id("https://example.com/post/1234567") == "id:example.com:1234567"
    assert knowledge.seen_id("https://example.com/about") == ""


def test_global_keepfrag_rule_is_used():
    knowledge = build(global_rule=parse_rule("* keepfrag=^(file|item)=|/file/|^!"))
    assert knowledge.canon("https://example.com/x#file=1") == "example.com/x#file=1"
    assert knowledge.canon("https://example.com/x#comments") == "example.com/x"


def test_dl_url_substitutes_a_mirror_and_keeps_the_path():
    knowledge = build(mirrors=MirrorTable(mirrors={"kemono.party": "pawchive.pw"}))
    assert knowledge.dl_url("https://kemono.party/user/1") == "https://pawchive.pw/user/1"


def test_dl_url_strips_tracking_but_keeps_download_arguments():
    url = "https://example.com/file?id=9&utm_source=x"
    assert dl_url(url) == "https://example.com/file?id=9"


def test_dl_url_keeps_arguments_canon_would_drop():
    # the download URL keeps every argument the engines might need; only the
    # canonical form (the archive key) narrows it to the item identity
    assert dl_url("https://example.com/file?id=9&usp=sharing").endswith("usp=sharing")


def test_playlist_rule_needs_all_three_keys():
    rule = parse_rule(
        "example.com playlist=user postid=[0-9]+ posturl=https://{host}/{user}/{id}"
    )
    knowledge = build({"example.com": rule})
    page = knowledge.playlist("https://example.com/fanbox/user/SomeOne")
    assert page is not None
    assert (page.host, page.service, page.user) == ("example.com", "fanbox", "SomeOne")
    assert knowledge.playlist("https://example.com/fanbox/user/SomeOne/photos") is None


def test_a_site_without_playlist_keys_is_not_a_creator_page():
    knowledge = build({"example.com": parse_rule("example.com keep=all")})
    assert knowledge.playlist("https://example.com/user/SomeOne") is None


def test_rule_lookup_matches_subdomains_and_also_hosts():
    knowledge = build({
        "example.com": parse_rule("example.com also=mirror.example"),
        "ecchi.example.com": parse_rule("ecchi.example.com keep=1"),
    })
    assert knowledge.rule("https://www.example.com/x").host == "example.com"
    assert knowledge.rule("https://deep.sub.example.com/x").host == "example.com"
    assert knowledge.rule("https://mirror.example/x").host == "example.com"
    # the most specific rule wins
    assert knowledge.rule("https://ecchi.example.com/x").host == "ecchi.example.com"


def test_dl_url_applies_a_site_rewrite():
    rule = parse_rule(
        r"ecchi.example rewrite=^https?://ecchi\.example/videos/(.*)$=>https://www.example/video/\1"
    )
    knowledge = build({"ecchi.example": rule})
    assert knowledge.dl_url("https://ecchi.example/videos/abc") == (
        "https://www.example/video/abc"
    )
