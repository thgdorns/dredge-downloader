"""Compare a gallery-dl post's attachments with the files on disk.

The URL shown for a pawchive/kemono attachment is a signed CDN link that
changes on every request, but the extractor also exposes the content ``hash``
(SHA-256). A changed file gets a new hash; a text/title-only edit does not.
fantia has no content hash; its stable identity is ``file_id``.

gallery-dl's default ``metadata`` postprocessor writes a ``<media>.json``
sidecar next to every downloaded file. That sidecar keeps the original hash or
file id even when pawchive only served a preview. ``--refresh`` compares those
sidecars with the post's current files and stops when every remote file is
already recorded; otherwise it re-downloads the whole post with ``--no-skip``,
overwriting the old copies in place.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .engines import run

_HASH = re.compile(r"^[0-9A-Za-z_-]{8,128}$")


@dataclass(frozen=True)
class PostMeta:
    identities: tuple[str, ...] = ()
    post_id: str = ""
    complete: bool = True


@dataclass(frozen=True)
class Folder:
    url: str
    folder: str


def _json_payload(text: str) -> object | None:
    """Parse gallery-dl's ``-j`` output, ignoring any log lines around it."""
    try:
        return json.loads(text.strip())
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        return value
    return None


def parse_metadata(text: str) -> PostMeta | None:
    """Attachment identities of one post from ``gallery-dl -j`` output.

    ``h:<sha256>`` for extractors with a content hash, ``f:<file_id>`` for
    fantia. A creator page yields several post ids and returns ``None``; a file
    without either identity makes ``complete`` false so the caller re-downloads
    instead of guessing.
    """
    data = _json_payload(text)
    if data is None:
        return None
    post_ids: set[str] = set()
    identities: list[str] = []
    complete = True

    def walk(node: object) -> None:
        nonlocal complete
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "num" in node:
                value = node.get("hash")
                file_id = node.get("file_id")
                if isinstance(value, str) and _HASH.match(value):
                    identities.append("h:" + value)
                elif file_id not in (None, ""):
                    identities.append("f:" + str(file_id))
                else:
                    complete = False
            else:
                post_id = node.get("id") or node.get("post_id")
                if post_id and (
                    "title" in node or "post_title" in node
                    or "edited" in node or "published" in node or "posted_at" in node
                ):
                    post_ids.add(str(post_id))
            for item in node.values():
                walk(item)

    walk(data)
    if len(post_ids) != 1:
        return None
    return PostMeta(
        identities=tuple(identities),
        post_id=next(iter(post_ids)),
        complete=complete,
    )


def fetch_post(engine: str, paths, url: str, *, timeout: float = 180.0) -> PostMeta | None:
    """Ask gallery-dl for a post's attachment identities, without downloading."""
    args = [
        engine, "--config-ignore", "-c", str(paths.gallery_conf),
        "-q", "-j",
    ]
    if getattr(paths, "gallery_runtime", None) and paths.gallery_runtime.exists():
        args += ["-c", str(paths.gallery_runtime)]
    args.append(url)
    rc, out = run(args, cwd=paths.root, timeout=timeout)
    if rc != 0:
        return None
    return parse_metadata(out)


def _sidecar_identities(folder: Path) -> Counter[str]:
    """Hash/file-id identities recorded by gallery-dl's metadata sidecars."""
    found: Counter[str] = Counter()
    try:
        sidecars = list(folder.glob("*.json"))
    except OSError:
        return found
    for path in sidecars:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        value = data.get("hash")
        if isinstance(value, str) and _HASH.match(value):
            found["h:" + value] += 1
            continue
        file_id = data.get("file_id")
        if file_id not in (None, ""):
            found["f:" + str(file_id)] += 1
    return found


def up_to_date(meta: PostMeta, folder: str | Path) -> bool:
    """True when every remote attachment is recorded by a local sidecar."""
    if not meta.complete:
        return False
    target = Path(folder)
    if not target.is_dir():
        return False
    if not meta.identities:
        return True
    kinds = {identity.split(":", 1)[0] for identity in meta.identities}
    if not kinds <= {"h", "f"}:
        return False
    remote = Counter(meta.identities)
    local = _sidecar_identities(target)
    return bool(local) and remote <= local


def load(path: str | Path) -> dict[str, Folder]:
    """Read the folder ledger (URL -> local post folder)."""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, Folder] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or not fields[1]:
            continue
        out[fields[0]] = Folder(fields[0], fields[1])
    return out


def save(path: str | Path, folders: dict[str, Folder]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            f"{row.url}\t{row.folder}\r\n" for row in folders.values()
        )
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(p)
    except OSError:
        pass


def remember_folder(path: str | Path, url: str, folder: str) -> None:
    """Append the local folder of a post (last row wins)."""
    if not folder:
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{url}\t{folder}\r\n")
    except OSError:
        pass


def manifest_folder(manifest: str | Path) -> str:
    """The post folder of the first file a gallery-dl run wrote."""
    try:
        for line in Path(manifest).read_text(encoding="utf-8", errors="replace").splitlines():
            source = line.strip()
            if source:
                return str(Path(source).parent)
    except OSError:
        pass
    return ""
