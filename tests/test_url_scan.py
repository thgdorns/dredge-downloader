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
    assert not module.is_sidecar(tmp_path / "metadata.json")
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
