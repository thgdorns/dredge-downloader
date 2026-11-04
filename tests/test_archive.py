from dredge.archive import Row, append_row, parse_row, read_rows


def test_parse_round_trip():
    row = parse_row("2026-01-01 00:00:00\texample.com/a\tA.mp4;B.mp4\t10;20")
    assert row == Row("2026-01-01 00:00:00", "example.com/a", ("A.mp4", "B.mp4"), (10, 20))


def test_legacy_three_column_row():
    row = parse_row("2026-01-01 00:00:00\texample.com/a\tA.mp4")
    assert row is not None and row.sizes == ()


def test_append_and_read(tmp_path):
    path = tmp_path / "downloads.tsv"
    append_row(path, Row("2026-01-01 00:00:00", "example.com/a", ("A.mp4",), (10,)))
    rows = list(read_rows(path))
    assert rows[0].names == ("A.mp4",)
