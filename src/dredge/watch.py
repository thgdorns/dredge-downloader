"""Audit the workarounds in this project against the engines it drives.

Every workaround exists for a reason that can expire: an engine gains the
feature the workaround imitates, an upstream issue closes, a site changes its
behaviour. This module keeps that knowledge in ``workarounds.json`` and checks
it, so a scheduled run can say "this one is no longer needed" instead of nobody
remembering.

Verdicts per workaround:

- ``KEEP``    - the reason still applies (no list flag, extractor still generic,
                issue still open, manual re-check not due).
- ``RETIRE``  - the reason is gone: read the evidence, then delete the
                workaround and its registry entry.
- ``DUE``     - a manual re-check has expired (timings, cookie behaviour).
- ``UNKNOWN`` - the engine is not installed or the check could not run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .engines import resolve_engine

KEEP = "KEEP"
RETIRE = "RETIRE"
DUE = "DUE"
UNKNOWN = "UNKNOWN"

_ORDER = {RETIRE: 0, DUE: 1, UNKNOWN: 2, KEEP: 3}
_ISSUE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")

Runner = Callable[[Sequence[str], float], tuple[int, str]]
Fetcher = Callable[[str], dict | None]


@dataclass(frozen=True)
class Check:
    kind: str
    engine: str = ""
    flags: tuple[str, ...] = ()
    url: str = ""
    expect_not: str = ""
    timeout: float = 60.0
    recheck_after_days: int = 0
    last_verified: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Check:
        kind = str(raw.get("kind", ""))
        if kind not in ("help_flag", "extractor", "issue", "manual"):
            raise ValueError(f"unknown check kind: {kind!r}")
        return cls(
            kind=kind,
            engine=str(raw.get("engine", "")),
            flags=tuple(str(f) for f in raw.get("flags", ())),
            url=str(raw.get("url", "")),
            expect_not=str(raw.get("expect_not", "")),
            timeout=float(raw.get("timeout", 60.0)),
            recheck_after_days=int(raw.get("recheck_after_days", 0)),
            last_verified=str(raw.get("last_verified", "")),
        )


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    engine: str
    added: str
    where: tuple[str, ...]
    retire_when: str
    checks: tuple[Check, ...]
    issue: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Entry:
        checks = tuple(Check.from_dict(item) for item in raw.get("checks", ()))
        if not checks:
            raise ValueError(f"{raw.get('id')!r} has no checks")
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            engine=str(raw.get("engine", "")),
            added=str(raw.get("added", "")),
            where=tuple(str(w) for w in raw.get("where", ())),
            retire_when=str(raw.get("retire_when", "")),
            checks=checks,
            issue=str(raw.get("issue", "")),
        )


@dataclass
class Finding:
    entry: Entry
    verdict: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Report:
    """An upstream issue we filed; tracked for visibility, not for action."""

    id: str
    url: str
    title: str = ""


def _read(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_registry(path: str | Path) -> list[Entry]:
    return [Entry.from_dict(raw) for raw in _read(path)["entries"]]


def load_reports(path: str | Path) -> list[Report]:
    return [
        Report(id=str(raw["id"]), url=str(raw["url"]), title=str(raw.get("title", "")))
        for raw in _read(path).get("reports", ())
    ]


# -- default I/O -------------------------------------------------------------

def run_command(args: Sequence[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def fetch_json(url: str) -> dict | None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "dredge-engine-watch", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def resolve_exe(engine: str) -> str | None:
    exe = "gallery-dl.exe" if engine == "gallery-dl" else "yt-dlp.exe"
    if sys.platform != "win32":
        exe = exe.removesuffix(".exe")
    return resolve_engine(exe)


# -- checks ------------------------------------------------------------------

def check_help_flag(check: Check, exe: str | None, runner: Runner) -> tuple[str, str]:
    if not exe:
        return UNKNOWN, f"{check.engine} is not installed"
    rc, text = runner([exe, "--help"], 60.0)
    if rc != 0 or not text.strip():
        return UNKNOWN, f"{check.engine} --help failed (exit {rc})"
    found = [
        flag for flag in check.flags
        if re.search(re.escape(flag) + r"(?![\w-])", text)
    ]
    if found:
        return RETIRE, f"{check.engine} --help now offers {', '.join(found)}"
    return KEEP, f"{check.engine} --help has none of: {', '.join(check.flags)}"


def check_extractor(check: Check, exe: str | None, runner: Runner) -> tuple[str, str]:
    if not exe:
        return UNKNOWN, f"{check.engine} is not installed"
    rc, text = runner(
        [
            exe, "--ignore-config", "--no-warnings", "--simulate",
            "--print", "%(extractor)s", check.url,
        ],
        check.timeout,
    )
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("[")
    ]
    extractor = lines[-1].lower() if lines else ""
    if rc != 0 or not extractor:
        tail = text.strip().splitlines()[-1] if text.strip() else f"exit {rc}"
        return UNKNOWN, f"could not extract {check.url}: {tail[:160]}"
    if extractor != check.expect_not.lower():
        return RETIRE, f"{check.url} is handled by `{extractor}`, not `{check.expect_not}`"
    return KEEP, f"{check.url} still falls back to `{extractor}`"


def check_issue(check: Check, fetcher: Fetcher) -> tuple[str, str]:
    match = _ISSUE_RE.search(check.url)
    if not match:
        return UNKNOWN, f"not a GitHub issue URL: {check.url}"
    owner, repo, number = match.groups()
    data = fetcher(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}")
    if not data:
        return UNKNOWN, f"could not read {check.url}"
    state = str(data.get("state", "")).lower()
    if state == "closed":
        return RETIRE, f"{check.url} is closed"
    return KEEP, f"{check.url} is still {state or 'open'}"


def report_state(report: Report, offline: bool, fetcher: Fetcher = fetch_json) -> str:
    """GitHub state of an upstream report, or ``?`` when it cannot be read."""
    if offline:
        return "?"
    match = _ISSUE_RE.search(report.url)
    if not match:
        return "?"
    owner, repo, number = match.groups()
    data = fetcher(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}")
    return str(data.get("state", "?")) if data else "?"


def check_manual(check: Check, today: date) -> tuple[str, str]:
    if not check.last_verified:
        return DUE, "no verification date recorded"
    try:
        verified = date.fromisoformat(check.last_verified)
    except ValueError:
        return UNKNOWN, f"bad last_verified: {check.last_verified!r}"
    age = (today - verified).days
    if check.recheck_after_days and age >= check.recheck_after_days:
        return DUE, f"last verified {check.last_verified}, {age} days ago"
    return KEEP, f"verified {check.last_verified} ({age} days ago)"


def evaluate(
    entry: Entry,
    *,
    today: date,
    runner: Runner = run_command,
    fetcher: Fetcher = fetch_json,
    offline: bool = False,
) -> Finding:
    verdicts: list[str] = []
    evidence: list[str] = []
    for check in entry.checks:
        if check.kind == "manual":
            verdict, note = check_manual(check, today)
        elif check.kind == "issue":
            verdict, note = (UNKNOWN, "offline") if offline else check_issue(check, fetcher)
        elif check.kind == "help_flag":
            verdict, note = check_help_flag(check, resolve_exe(check.engine), runner)
        else:
            verdict, note = check_extractor(check, resolve_exe(check.engine), runner)
        verdicts.append(verdict)
        evidence.append(f"{check.kind}: {note}")
    if RETIRE in verdicts:
        final = RETIRE
    elif DUE in verdicts:
        final = DUE
    elif KEEP not in verdicts:
        final = UNKNOWN
    else:
        final = KEEP
    return Finding(entry=entry, verdict=final, evidence=evidence)


# -- report ------------------------------------------------------------------

def engine_versions(
    runner: Runner = run_command, fetcher: Fetcher = fetch_json, offline: bool = False
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for engine, repo in (("gallery-dl", "mikf/gallery-dl"), ("yt-dlp", "yt-dlp/yt-dlp")):
        exe = resolve_exe(engine)
        installed = ""
        if exe:
            _rc, text = runner([exe, "--version"], 30.0)
            installed = text.strip().splitlines()[0] if text.strip() else ""
        latest = ""
        if not offline:
            data = fetcher(f"https://api.github.com/repos/{repo}/releases/latest")
            if data:
                latest = str(data.get("tag_name", "")).lstrip("v")
        out[engine] = {"installed": installed, "latest": latest}
    return out


def render_markdown(
    findings: Iterable[Finding],
    versions: dict[str, dict[str, str]],
    today: date,
    reports: Iterable[tuple[str, Report]] = (),
) -> str:
    findings = sorted(findings, key=lambda f: (_ORDER[f.verdict], f.entry.id))
    action = [f for f in findings if f.verdict in (RETIRE, DUE)]
    lines = [
        "# engine-watch",
        "",
        (f"Checked {today.isoformat()}. "
         f"{len(findings)} workaround(s), {len(action)} needing action."),
        "",
        "| engine | installed | latest release |",
        "| --- | --- | --- |",
    ]
    for engine, data in versions.items():
        lines.append(f"| {engine} | {data['installed'] or '?'} | {data['latest'] or '?'} |")
    for finding in findings:
        entry = finding.entry
        lines += [
            "",
            f"## {finding.verdict} - {entry.id}",
            "",
            f"**{entry.title}**",
            "",
            f"- engine: {entry.engine}",
            f"- retire when: {entry.retire_when}",
            f"- where: {', '.join(f'`{w}`' for w in entry.where)}",
        ]
        if entry.issue:
            lines.append(f"- issue: {entry.issue}")
        for note in finding.evidence:
            lines.append(f"- {note}")
        if finding.verdict == RETIRE:
            lines.append(
                "- action: delete the workaround and its registry entry, "
                "then run the tests"
            )
    if reports:
        lines += ["", "## Upstream reports", "", "| state | report |", "| --- | --- |"]
        for state, report in reports:
            label = report.title or report.id
            lines.append(f"| {state} | [{report.id}]({report.url}) - {label} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dredge-watch",
        description="Check whether a workaround is still needed after an engine update.",
    )
    parser.add_argument("--registry", default=None, help="workarounds.json to read")
    parser.add_argument("--today", default=None, metavar="YYYY-MM-DD",
                        help="date to evaluate manual checks against (default: today)")
    parser.add_argument("--offline", action="store_true",
                        help="skip network checks (issue states, latest releases)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    registry = Path(args.registry) if args.registry else Path(__file__).with_name("workarounds.json")
    try:
        entries = load_registry(registry)
        reports = load_reports(registry)
    except (OSError, ValueError, KeyError) as exc:
        print(f"engine-watch: cannot read {registry}: {exc}", file=sys.stderr)
        return 2

    today = (
        date.fromisoformat(args.today) if args.today
        else datetime.now().astimezone().date()
    )
    findings = [evaluate(entry, today=today, offline=args.offline) for entry in entries]
    versions = engine_versions(offline=args.offline)
    report_states = [(report_state(report, args.offline), report) for report in reports]

    if args.json:
        print(json.dumps(
            {
                "checked": today.isoformat(),
                "engines": versions,
                "findings": [
                    {
                        "id": f.entry.id,
                        "verdict": f.verdict,
                        "title": f.entry.title,
                        "evidence": f.evidence,
                    }
                    for f in findings
                ],
                "reports": [
                    {"id": report.id, "state": state, "url": report.url}
                    for state, report in report_states
                ],
            },
            indent=2,
        ))
    else:
        print(render_markdown(findings, versions, today, report_states), end="")

    return 1 if any(f.verdict in (RETIRE, DUE) for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
