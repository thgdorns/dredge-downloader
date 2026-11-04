"""The queue filter: what actually gets downloaded from a queue file.

This is the decision the legacy script made inline at the head of every pass,
extracted so it can be tested and so both implementations can be compared on
the same input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .destinations import destination
from .urls import Knowledge

_HTTP = re.compile(r"(?i)^https?://")
_UNUSABLE = re.compile(r"[<>\s]")


@dataclass
class Plan:
    urls: list[str] = field(default_factory=list)
    downloaded: int = 0
    marked_skip: int = 0
    failed_skip: int = 0
    duplicates: int = 0
    unusable: int = 0

    @property
    def skipped(self) -> int:
        return (
            self.downloaded + self.marked_skip + self.failed_skip
            + self.duplicates + self.unusable
        )

    def summary(self) -> str:
        """The ``skipped: ...`` line the legacy console prints."""
        parts: list[str] = []
        if self.downloaded:
            parts.append(f"{self.downloaded} already downloaded")
        if self.marked_skip:
            parts.append(f"{self.marked_skip} in skip.txt (never retried)")
        if self.failed_skip:
            parts.append(f"{self.failed_skip} known-failed (-RetryFailed retries them)")
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate line(s) dropped")
        if self.unusable:
            parts.append(f"{self.unusable} unusable line(s) dropped")
        return "skipped: " + ", ".join(parts) + "." if parts else ""


def build_plan(
    urls: list[str],
    *,
    seen: set[str],
    failed: set[str],
    skip_always: set[str],
    knowledge: Knowledge,
    archive_check: bool = True,
    retry_failed: bool = False,
) -> Plan:
    """Drop unusable, downloaded, skipped, known-failed and duplicate lines.

    Order is preserved. ``archive_check=False`` (menu 1 -> 1) ignores every
    record: nothing is skipped, and the caller is expected to tell the engines
    to ignore their own archives as well.
    """
    plan = Plan()
    unique: set[str] = set()
    for raw in urls:
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        if not _HTTP.match(url) or _UNUSABLE.search(url):
            plan.unusable += 1
            continue
        c = knowledge.canon(url)
        if not c:
            continue
        ident = knowledge.seen_id(c)
        if archive_check and c in seen:
            plan.downloaded += 1
            continue
        if archive_check and ident and ident in seen:
            plan.downloaded += 1
            continue
        if archive_check and (
            c in skip_always or url in skip_always or (ident and ident in skip_always)
        ):
            plan.marked_skip += 1
            continue
        if archive_check and not retry_failed and (
            c in failed or url in failed or (ident and ident in failed)
        ):
            plan.failed_skip += 1
            continue
        if c in unique:
            plan.duplicates += 1
            continue
        unique.add(c)
        plan.urls.append(url)
    return plan


def order_by_destination(
    urls: list[str],
    *,
    settings,
    domains: tuple[str, ...],
) -> tuple[list[str], int, int]:
    """Non-porn links first; returns ``(ordered, non_porn, porn)``.

    They are the bulk of a queue and land in per-creator folders, so they must
    not wait behind a long tail of adult links.
    """
    non_porn: list[str] = []
    porn: list[str] = []
    for url in urls:
        if destination(url, settings, domains) == settings.dest_porn:
            porn.append(url)
        else:
            non_porn.append(url)
    return non_porn + porn, len(non_porn), len(porn)


def read_queue(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip().lstrip("\ufeff").strip()
        if text and not text.startswith("#"):
            out.append(text)
    return out
