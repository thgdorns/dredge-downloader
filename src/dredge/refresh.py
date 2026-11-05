"""Revision-aware refresh of a gallery-dl post.

gallery-dl records one archive entry per file position
(``{service}_{user}_{id}_{num}``), so a post revision that replaces the file at
an existing position looks like a duplicate and stays skipped. A refresh first
reads the post metadata without downloading, compares the post's ``edited``
stamp and the content hashes of its files with the last refresh, and then asks
gallery-dl for only the files whose hash is new. ``--no-skip`` makes those
overwrite the old copy in place, so no ``name.1.ext`` duplicates appear.

The last-seen revision lives in ``data/state/revisions.tsv``; it is small and
only holds posts that were downloaded or refreshed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .engines import run

# A content hash from kemono/pawchive metadata. The value is used inside a
# gallery-dl ``--filter`` expression, so only plain identifier characters pass.
_HASH = re.compile(r"^[0-9A-Za-z_-]{8,128}$")

# A Windows command line is ~32k characters. Keep the filter well below it and
# fall back to a full refresh for very large posts.
_FILTER_LIMIT = 20_000


@dataclass(frozen=True)
class PostMeta:
    edited: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class Revision:
    url: str
    edited: str
    hashes: tuple[str, ...]
    folder: str = ""


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
    """One post and its file hashes from ``gallery-dl -j`` output.

    A creator page yields several post dictionaries; revision tracking is only
    meaningful for a single post, so that case returns ``None`` and the caller
    does a full refresh.
    """
    data = _json_payload(text)
    if data is None:
        return None
    posts: list[dict] = []
    hashes: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            value = node.get("hash")
            if isinstance(value, str) and "num" in node:
                if _HASH.match(value):
                    hashes.append(value)
            elif "id" in node and "edited" in node and "num" not in node:
                posts.append(node)

    walk(data)
    if len(posts) != 1:
        return None
    post = posts[0]
    edited = str(
        post.get("edited") or post.get("updated") or post.get("added") or ""
    )
    return PostMeta(edited=edited, hashes=tuple(dict.fromkeys(hashes)))


def fetch_post(engine: str, paths, url: str, *, timeout: float = 180.0) -> PostMeta | None:
    """Ask gallery-dl for a post's metadata, without downloading anything."""
    args = [engine, "--config-ignore", "-c", str(paths.gallery_conf), "-q", "-j"]
    if getattr(paths, "gallery_runtime", None) and paths.gallery_runtime.exists():
        args += ["-c", str(paths.gallery_runtime)]
    args.append(url)
    rc, out = run(args, cwd=paths.root, timeout=timeout)
    if rc != 0:
        return None
    return parse_metadata(out)


def load(path: str | Path) -> dict[str, Revision]:
    """Read the revision ledger (URL -> edited stamp + content hashes)."""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, Revision] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or not fields[0]:
            continue
        hashes = tuple(h for h in fields[2].split(";") if h)
        folder = fields[3] if len(fields) > 3 else ""
        out[fields[0]] = Revision(fields[0], fields[1], hashes, folder)
    return out


def save(path: str | Path, revisions: dict[str, Revision]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            f"{row.url}\t{row.edited}\t{';'.join(row.hashes)}\t{row.folder}\r\n"
            for row in revisions.values()
        )
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(p)
    except OSError:
        pass


def remember(
    path: str | Path,
    url: str,
    edited: str,
    hashes: tuple[str, ...],
    folder: str = "",
) -> None:
    """Store the revision a successful run just saw.

    Appended rather than rewritten: a download must not rewrite a ledger that
    grows with the archive. :func:`load` keeps the last row for a URL.
    """
    row = Revision(url, edited, tuple(dict.fromkeys(hashes)), folder)
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="") as fh:
            fh.write(
                f"{row.url}\t{row.edited}\t{';'.join(row.hashes)}\t{row.folder}\r\n"
            )
    except OSError:
        pass


def hash_filter(hashes: tuple[str, ...]) -> str:
    """A gallery-dl filter that drops every file whose content is unchanged."""
    known = [h for h in dict.fromkeys(hashes) if _HASH.match(h)]
    if not known:
        return ""
    expression = "hash not in (" + ",".join(f"'{h}'" for h in known) + ")"
    return expression if len(expression) <= _FILTER_LIMIT else ""


def post_json_hashes(text: str) -> tuple[str, ...]:
    """Every content hash inside a ``post.json`` sidecar."""
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            value = node.get("hash")
            if isinstance(value, str) and _HASH.match(value):
                found.append(value)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return tuple(dict.fromkeys(found))


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


def manifest_hashes(manifest: str | Path) -> tuple[str, ...]:
    """Hashes gallery-dl printed for the files it actually wrote.

    ``{hash}`` is written next to the ``{_path}`` manifest for every moved
    file, so the ledger only records content that is really on disk. A post
    attachment that failed or is still pending is absent and stays eligible
    for the next refresh.
    """
    path = Path(str(manifest) + ".hashes")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    found = [line.strip() for line in text.splitlines() if _HASH.match(line.strip())]
    return tuple(dict.fromkeys(found))


def remember_from_manifest(path: str | Path, url: str, manifest: Path) -> bool:
    """Seed the ledger from what a normal download actually wrote.

    A ``post.json`` sidecar, when the configuration writes one, supplies the
    ``edited`` stamp; the hashes come from gallery-dl's own per-file output,
    never from the post's attachment list (which can include files this run did
    not get).
    """
    hashes = manifest_hashes(manifest)
    folder = manifest_folder(manifest)
    if not hashes or not folder:
        return False
    edited = ""
    sidecar = Path(folder) / "post.json"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        pass
    else:
        edited = str(data.get("edited") or data.get("updated") or "")
    remember(path, url, edited, hashes, folder)
    return True
