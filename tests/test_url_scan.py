"""The URL scan tool: what a default run picks up and what it leaves alone."""

import importlib.util
import json
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "url_scan.py"


def load():
    spec = importlib.util.spec_from_file_location("url_scan_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_sidecar_json_is_recognised(tmp_path):
    module = load()

    assert module.is_sidecar(tmp_path / "photo.jpg.json")
    assert module.is_sidecar(tmp_path / "clip.mp4.json")
    assert module.is_sidecar(tmp_path / "post.json")
    assert module.is_sidecar(tmp_path / "some" / "post.json")
    assert not module.is_sidecar(tmp_path / "metadata.json")
    assert not module.is_sidecar(tmp_path / "Shu.upgrades.json")
    assert not module.is_sidecar(tmp_path / "clip.mp4")


def test_the_default_scan_reads_descriptions_and_sidecar_json(tmp_path):
    module = load()
    for name in ("a.jpg.json", "b.json", "c.mp4.description", "d.txt"):
        (tmp_path / name).write_text("{}", encoding="utf-8")

    files = module.iter_files(
        tmp_path, set(module.DEFAULT_EXTENSIONS), True, [], True
    )

    assert [path.name for path in files] == ["a.jpg.json", "c.mp4.description"]


def test_an_explicit_extension_list_is_not_sidecar_filtered(tmp_path):
    module = load()
    for name in ("a.jpg.json", "b.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")

    files = module.iter_files(tmp_path, {".json"}, True, [], False)

    assert [path.name for path in files] == ["a.jpg.json", "b.json"]


def test_a_sidecar_body_is_the_content_field():
    module = load()
    sidecar = json.dumps({
        "id": "1",
        "content": "<p>source https://example.com/post</p>",
        "attachments": [{"path": "/x.jpg", "name": "x.jpg"}],
        "file": {"path": "/cover.jpg"},
    })

    assert module.json_content(sidecar) == "<p>source https://example.com/post</p>"
    assert module.json_content('{"id": "1", "attachments": []}') is None
    assert module.json_content("not json") is None


def test_the_scan_ignores_attachment_urls(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "a.jpg.json").write_text(json.dumps({
        "id": "1",
        "content": "<p>source https://example.com/post</p>",
        "attachments": [{"path": "/x/a.jpg", "name": "a.jpg"}],
        "file": {"path": "/x/cover.jpg"},
        "embed": {"url": "https://cdn.example.com/embed"},
    }), encoding="utf-8")

    code = module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q",
    ])

    assert code == 0
    report = next((tmp_path / "reports").glob("url-scan-*.tsv"))
    text = report.read_text(encoding="utf-8")
    assert "https://example.com/post" in text
    assert "cdn.example.com" not in text
    assert "/x/a.jpg" not in text


def test_a_password_hint_yields_its_value():
    module = load()

    assert module.extract_passwords("pass is xyz") == ["xyz"]
    assert module.extract_passwords("password: xy") == ["xy"]
    assert module.extract_passwords("password is xyz") == ["xyz"]
    assert module.extract_passwords("pass is the usual") == ["the usual"]


def test_password_hints_in_five_languages():
    module = load()

    assert module.extract_passwords("Contraseña: hola") == ["hola"]
    assert module.extract_passwords("パスワード：テスト") == ["テスト"]
    assert module.extract_passwords("解压密码：777") == ["777"]
    assert module.extract_passwords("密码是abc") == ["abc"]
    assert module.extract_passwords("비밀번호: 1234") == ["1234"]


def test_ordinary_words_are_not_passwords():
    module = load()

    assert module.extract_passwords("time sure does pass by quick") == []
    assert module.extract_passwords("they passed away") == []
    assert module.extract_passwords("password protected archive") == []
    assert module.extract_passwords("解压密码：\n视频动画和作者：Japs 8005") == []


def test_the_scan_writes_a_password_report(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "a.jpg.json").write_text(json.dumps({
        "content": "<p>Password: hunter2</p><p>https://example.com/post</p>",
        "attachments": [{"path": "/x/a.jpg", "name": "a.jpg"}],
    }), encoding="utf-8")

    code = module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q",
    ])

    assert code == 0
    reports = tmp_path / "reports"
    urls = next(reports.glob("url-scan-*.tsv")).read_text(encoding="utf-8")
    passwords = next(reports.glob("passwords-*.tsv")).read_text(encoding="utf-8")
    assert "https://example.com/post" in urls
    assert "hunter2" in passwords
    assert "example.com" not in passwords


def test_one_row_per_value_with_every_source(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    (downloads / "post").mkdir(parents=True)
    for name in ("a.jpg.json", "b.jpg.json"):
        (downloads / "post" / name).write_text(json.dumps({
            "content": "<p>same https://example.com/one plus https://example.com/two</p>",
        }), encoding="utf-8")

    module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q",
    ])

    rows = next((tmp_path / "reports").glob("url-scan-*.tsv")).read_text(
        encoding="utf-8").splitlines()
    assert len(rows) == 2
    first = rows[0].split("\t")
    assert first[0] == "https://example.com/one"
    assert len(first[1].split(";")) == 2      # both sidecars, one row


def test_file_host_links_are_queued_into_their_folder(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    (downloads / "author" / "123456").mkdir(parents=True)
    (downloads / "author" / "123456" / "a.jpg.json").write_text(json.dumps({
        "content": "<p>video https://drive.google.com/file/d/abc/view and "
                   "https://mega.nz/file/xyz#key and https://example.com/keep</p>",
    }), encoding="utf-8")

    module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q",
    ])

    queue = tmp_path / "data" / "queue.txt"
    queued = queue.read_text(encoding="utf-8").splitlines()
    folder = downloads / "author" / "123456"
    assert queued == [
        f"https://drive.google.com/file/d/abc/view\t{folder}",
        f"https://mega.nz/file/xyz#key\t{folder}",
    ]
    report = next((tmp_path / "reports").glob("url-scan-*.tsv")).read_text(
        encoding="utf-8")
    assert "example.com/keep" in report
    assert "drive.google.com" not in report
    assert "mega.nz" not in report


def test_no_queue_keeps_the_file_host_links_in_the_report(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "a.jpg.json").write_text(json.dumps({
        "content": "<p>https://drive.google.com/file/d/abc/view</p>",
    }), encoding="utf-8")

    module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q", "--no-queue",
    ])

    assert not (tmp_path / "data" / "queue.txt").exists()
    report = next((tmp_path / "reports").glob("url-scan-*.tsv")).read_text(
        encoding="utf-8")
    assert "drive.google.com/file/d/abc/view" in report


def test_a_link_already_queued_is_not_added_twice(tmp_path):
    module = load()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "a.jpg.json").write_text(json.dumps({
        "content": "<p>https://mega.nz/file/xyz#key</p>",
    }), encoding="utf-8")
    queue = tmp_path / "data" / "queue.txt"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "https://mega.nz/file/xyz#key\tG:\\somewhere\r\n",
        encoding="utf-8",
        newline="",
    )

    module.main([
        "--root", str(tmp_path), str(downloads),
        "--keep-metadata", "--no-open", "-q",
    ])

    assert queue.read_text(encoding="utf-8").splitlines() == [
        "https://mega.nz/file/xyz#key\tG:\\somewhere"
    ]
