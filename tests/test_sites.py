from dredge.sites import find_rule, parse_rule


def test_parse_full_line():
    rule = parse_rule(
        "iwara.tv  keep=all min=0 id=[A-Za-z0-9]{10,20} also=youtu.be native "
        "itemurl=https://{host}/video/{id}/{slug} engine=gdl author=yt-dlp "
        "yt=--write-description  # a comment"
    )
    assert rule is not None
    assert rule.host == "iwara.tv"
    assert rule.keep == 0
    assert rule.native is True
    assert rule.engine == "gdl"
    assert rule.author == "yt-dlp"
    assert rule.yt == ("--write-description",)
    assert rule.also == ("youtu.be",)


def test_comment_and_blank_lines():
    assert parse_rule("# comment") is None
    assert parse_rule("   ") is None


def test_most_specific_rule_wins():
    rules = {
        "iwara.tv": parse_rule("iwara.tv keep=all"),
        "ecchi.iwara.tv": parse_rule("ecchi.iwara.tv keep=all engine=gdl"),
    }
    assert find_rule(rules, "www.ecchi.iwara.tv").engine == "gdl"
    assert find_rule(rules, "www.iwara.tv").engine == ""


def test_rewrite_key_is_kept_verbatim():
    rule = parse_rule(
        r"ecchi.example rewrite=^https?://ecchi\.example/videos/(.*)$=>https://www.example/video/\1"
    )
    assert rule is not None
    assert rule.rewrite == (
        r"^https?://ecchi\.example/videos/(.*)$=>https://www.example/video/\1"
    )
