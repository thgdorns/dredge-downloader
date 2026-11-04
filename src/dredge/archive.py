"""Reader/writer for ``data/downloads.tsv``.

Columns: timestamp, URL, names (``;``), sizes in bytes (``;``). Legacy rows with
three columns are accepted: the URL is still there, the names/sizes may not be.

New rows are parked in ``data/state/fold-pending.tsv`` and merged in batches, so
a finished download never rewrites the whole archive. The merge keeps one row per
URL: a pending row for a URL already recorded grows that row with the names it
does not have yet (a post that gained an attachment), and a size of 0 - "never
measured" - is filled in.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .urls import Knowledge

_UTF8 = "utf-8"


@dataclass(frozen=True)
class Row:
    timestamp: str
    url: str
    names: tuple[str, ...] = ()
    sizes: tuple[int, ...] = ()


@dataclass(frozen=True)
class MergeResult:
    added: int = 0
    grown: int = 0

    @property
    def total(self) -> int:
        return self.added + self.grown


def _ints(values: list[str]) -> tuple[int, ...]:
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            out.append(0)
    return tuple(out)


def parse_row(line: str) -> Row | None:
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    fields = text.split("\t")
    if len(fields) < 2:
        return None
    names = tuple(n for n in fields[2].split(";") if n) if len(fields) > 2 else ()
    sizes = _ints(fields[3].split(";")) if len(fields) > 3 else ()
    return Row(timestamp=fields[0], url=fields[1], names=names, sizes=sizes)


def read_rows(path: str | Path) -> Iterator[Row]:
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding=_UTF8, errors="replace") as fh:
        for line in fh:
            row = parse_row(line)
            if row:
                yield row


def format_row(row: Row) -> str:
    names = ";".join(row.names)
    sizes = ";".join(str(n) for n in row.sizes)
    return f"{row.timestamp}\t{row.url}\t{names}\t{sizes}"


def append_row(path: str | Path, row: Row) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=_UTF8, newline="") as fh:
        fh.write(format_row(row) + "\r\n")


def append_pending(path: str | Path, rows: Iterable[str]) -> int:
    """Park finished rows in the append-only pending file; returns the count."""
    keep = [row for row in rows if row.strip()]
    if not keep:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=_UTF8, newline="") as fh:
        for row in keep:
            fh.write(row.rstrip("\r\n") + "\r\n")
    return len(keep)


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding=_UTF8, errors="replace") as fh:
        return [line.rstrip("\r\n") for line in fh if line.strip()]


def _write_lines(path: Path, lines: list[str]) -> None:
    text = "".join(line + "\r\n" for line in lines)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=_UTF8, newline="")
    tmp.replace(path)


def load_sets(path: str | Path, knowledge: Knowledge) -> tuple[set[str], set[str]]:
    """``(seen, archived)`` - answers "already downloaded?" for queue filtering.

    Keys are the stored URL as-is plus the site's ``id=`` identity. Stored URLs
    are never re-canonicalised (that would rewrite 160k rows on every start).
    """
    seen: set[str] = set()
    archived: set[str] = set()
    p = Path(path)
    if not p.exists():
        return seen, archived
    with p.open(encoding=_UTF8, errors="replace") as fh:
        for line in fh:
            tab = line.find("\t")
            if tab < 0:
                continue
            tab2 = line.find("\t", tab + 1)
            url = line[tab + 1:] if tab2 < 0 else line[tab + 1:tab2]
            url = url.rstrip("\r\n")
            if not url:
                continue
            seen.add(url)
            archived.add(url)
            ident = knowledge.seen_id(url)
            if ident:
                seen.add(ident)
                archived.add(ident)
    return seen, archived


def merge_pending(
    pending: str | Path,
    archive: str | Path,
    seen: set[str],
    archived: set[str],
    knowledge: Knowledge,
    *,
    write: bool = True,
) -> MergeResult:
    """Fold ``fold-pending.tsv`` into the archive (newest row on top)."""
    pending_p = Path(pending)
    archive_p = Path(archive)
    rows = _read_lines(pending_p)
    if not rows:
        return MergeResult()

    existing = _read_lines(archive_p)
    by_key: dict[str, int] = {}
    for index, line in enumerate(existing):
        fields = line.split("\t")
        if len(fields) < 2 or not fields[1]:
            continue
        if fields[1] not in by_key:
            by_key[fields[1]] = index
        ident = knowledge.seen_id(fields[1])
        if ident and ident not in by_key:
            by_key[ident] = index

    keep: list[str] = []
    grown = 0
    for row in rows:
        fields = row.split("\t")
        if len(fields) < 3 or not fields[1]:
            continue
        url = fields[1]
        ident = knowledge.seen_id(url)
        index = by_key.get(url)
        if index is None and ident:
            index = by_key.get(ident)

        if index is None:
            keep.append(row)
            seen.add(url)
            archived.add(url)
            if ident:
                seen.add(ident)
                archived.add(ident)
            continue

        old = existing[index].split("\t")
        while len(old) < 4:
            old.append("")
        names = [n for n in old[2].split(";") if n]
        sizes = [s for s in old[3].split(";") if s] if old[3] else []
        while len(sizes) < len(names):
            sizes.append("0")
        new_names = [n for n in fields[2].split(";") if n]
        new_sizes = [s for s in fields[3].split(";") if s] if len(fields) > 3 and fields[3] else []

        changed = False
        for pos, name in enumerate(new_names):
            size = new_sizes[pos] if pos < len(new_sizes) and new_sizes[pos] else "0"
            if name in names:
                at = names.index(name)
                if sizes[at] == "0" and size != "0":
                    sizes[at] = size
                    changed = True
            else:
                names.append(name)
                sizes.append(size)
                changed = True
        if changed:
            old[2] = ";".join(names)
            old[3] = ";".join(sizes)
            existing[index] = "\t".join(old)
            grown += 1

    if write:
        if keep or grown:
            # pending holds rows oldest-first, the archive is newest-first
            _write_lines(archive_p, list(reversed(keep)) + existing)
        _write_lines(pending_p, [])
    return MergeResult(added=len(keep), grown=grown)
