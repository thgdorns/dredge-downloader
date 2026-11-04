from dredge.clipboard import extract_urls


def test_extract_urls_dedupes_and_trims_punctuation():
    text = "look: https://example.com/a). and https://example.com/b?x=1, twice https://example.com/a"
    assert extract_urls(text) == ["https://example.com/a", "https://example.com/b?x=1"]


def test_ignores_non_urls():
    assert extract_urls("no links here") == []
