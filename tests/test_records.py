from dredge.records import read_lines


def test_read_lines_handles_bom_comments_and_blanks(tmp_path):
    path = tmp_path / "skip.txt"
    path.write_text("\ufeff# comment\n\nhttps://example.com/a\n  https://example.com/b  \n",
                    encoding="utf-8")
    assert read_lines(path) == ["https://example.com/a", "https://example.com/b"]


def test_missing_file_is_empty(tmp_path):
    assert read_lines(tmp_path / "nope.txt") == []
