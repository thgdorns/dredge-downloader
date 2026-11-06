from datetime import date
from pathlib import Path

from dredge import watch

TODAY = date(2026, 11, 4)


def entry(*checks: dict, issue: str = "") -> watch.Entry:
    return watch.Entry(
        id="test",
        title="a workaround",
        engine="yt-dlp",
        added="2026-01-01",
        where=("src/dredge/worker.py",),
        retire_when="the engine does it itself",
        checks=tuple(watch.Check.from_dict(c) for c in checks),
        issue=issue,
    )


def runner(responses):
    def run(args, timeout):
        return responses(args)
    return run


def test_help_flag_retires_when_the_option_appears():
    check = watch.Check.from_dict(
        {"kind": "help_flag", "engine": "gallery-dl", "flags": ["--list"]}
    )
    verdict, note = watch.check_help_flag(
        check, "gallery-dl", runner(lambda a: (0, "-g, --get-urls\n    --list   print items"))
    )
    assert verdict == watch.RETIRE
    assert "--list" in note


def test_help_flag_keeps_when_only_similar_words_exist():
    check = watch.Check.from_dict(
        {"kind": "help_flag", "engine": "gallery-dl", "flags": ["--list"]}
    )
    verdict, _ = watch.check_help_flag(
        check, "gallery-dl", runner(lambda a: (0, "  --listen   not this one"))
    )
    assert verdict == watch.KEEP


def test_help_flag_without_the_engine_is_unknown():
    check = watch.Check.from_dict({"kind": "help_flag", "flags": ["--list"]})
    verdict, _ = watch.check_help_flag(check, None, runner(lambda a: (0, "")))
    assert verdict == watch.UNKNOWN


def test_extractor_retires_when_the_site_extractor_matches():
    check = watch.Check.from_dict(
        {"kind": "extractor", "engine": "yt-dlp", "url": "https://br.example/watch/1",
         "expect_not": "generic"}
    )
    verdict, note = watch.check_extractor(
        check, "yt-dlp", runner(lambda a: (0, "ExampleSite\n"))
    )
    assert verdict == watch.RETIRE
    assert "examplesite" in note


def test_extractor_keeps_while_it_falls_back_to_generic():
    check = watch.Check.from_dict(
        {"kind": "extractor", "engine": "yt-dlp", "url": "https://br.example/watch/1",
         "expect_not": "generic"}
    )
    verdict, _ = watch.check_extractor(
        check, "yt-dlp", runner(lambda a: (0, "[debug] something\ngeneric\n"))
    )
    assert verdict == watch.KEEP


def test_extractor_failure_is_unknown_not_retire():
    check = watch.Check.from_dict(
        {"kind": "extractor", "engine": "yt-dlp", "url": "https://br.example/watch/1",
         "expect_not": "generic"}
    )
    verdict, notes = watch.check_extractor(
        check, "yt-dlp", runner(lambda a: (1, "ERROR: blocked in your country"))
    )
    assert verdict == watch.UNKNOWN
    assert "blocked" in notes


def test_issue_retires_when_closed():
    check = watch.Check.from_dict(
        {"kind": "issue", "url": "https://github.com/o/r/issues/7"}
    )
    verdict, note = watch.check_issue(check, lambda url: {"state": "closed"})
    assert verdict == watch.RETIRE
    assert "closed" in note


def test_issue_keeps_while_open():
    check = watch.Check.from_dict(
        {"kind": "issue", "url": "https://github.com/o/r/issues/7"}
    )
    verdict, _ = watch.check_issue(check, lambda url: {"state": "open"})
    assert verdict == watch.KEEP


def test_manual_check_becomes_due_after_the_interval():
    check = watch.Check.from_dict(
        {"kind": "manual", "recheck_after_days": 30, "last_verified": "2026-10-01"}
    )
    verdict, _ = watch.check_manual(check, TODAY)
    assert verdict == watch.DUE


def test_manual_check_stays_keep_before_the_interval():
    check = watch.Check.from_dict(
        {"kind": "manual", "recheck_after_days": 30, "last_verified": "2026-11-01"}
    )
    verdict, _ = watch.check_manual(check, TODAY)
    assert verdict == watch.KEEP


def test_retire_wins_over_other_checks():
    item = entry(
        {"kind": "issue", "url": "https://github.com/o/r/issues/7"},
        {"kind": "manual", "recheck_after_days": 30, "last_verified": "2020-01-01"},
    )
    finding = watch.evaluate(
        item, today=TODAY, fetcher=lambda url: {"state": "closed"}
    )
    assert finding.verdict == watch.RETIRE


def test_report_puts_retire_first_and_names_the_action():
    gone = entry({"kind": "issue", "url": "https://github.com/o/r/issues/1"})
    kept = entry({"kind": "manual", "last_verified": "2026-11-01"})
    findings = [
        watch.Finding(kept, watch.KEEP, ["manual: verified"]),
        watch.Finding(gone, watch.RETIRE, ["issue: closed"]),
    ]
    text = watch.render_markdown(
        findings, {"yt-dlp": {"installed": "1", "latest": "2"}}, TODAY
    )
    assert text.index("RETIRE") < text.index("KEEP")
    assert "delete the workaround" in text
    assert "| yt-dlp | 1 | 2 |" in text


def test_registry_shipped_with_the_package_is_valid():
    path = Path(watch.__file__).with_name("workarounds.json")
    entries = watch.load_registry(path)
    assert entries
    assert all(entry.checks for entry in entries)
    assert len({entry.id for entry in entries}) == len(entries)


def test_offline_issue_check_is_unknown():
    item = entry({"kind": "issue", "url": "https://github.com/o/r/issues/1"})
    finding = watch.evaluate(item, today=TODAY, offline=True)
    assert finding.verdict == watch.UNKNOWN
    assert finding.evidence == ["issue: offline"]


def test_reports_are_loaded_from_the_shipped_registry():
    path = Path(watch.__file__).with_name("workarounds.json")
    reports = watch.load_reports(path)
    assert len(reports) == 3
    assert all(report.url.startswith("https://github.com/mikf/gallery-dl/") for report in reports)


def test_report_state_is_unknown_offline_and_read_online():
    report = watch.Report("yt-dlp#1", "https://github.com/o/r/issues/1")
    assert watch.report_state(report, offline=True) == "?"
    assert watch.report_state(
        report, offline=False, fetcher=lambda url: {"state": "closed"}
    ) == "closed"


def test_report_table_is_rendered():
    report = watch.Report("yt-dlp#1", "https://github.com/o/r/issues/1", "a report")
    text = watch.render_markdown([], {}, TODAY, [("open", report)])
    assert "## Upstream reports" in text
    assert "| open | [yt-dlp#1](https://github.com/o/r/issues/1) - a report |" in text


def test_unsupported_keeps_while_the_engine_refuses_the_url():
    check = watch.Check.from_dict(
        {"kind": "unsupported", "engine": "yt-dlp", "url": "https://iwara.ai/video/x"}
    )
    verdict, note = watch.check_unsupported(
        check,
        "yt-dlp",
        runner(lambda a: (1, "ERROR: Unsupported URL: https://iwara.ai/video/x")),
    )
    assert verdict == watch.KEEP
    assert "Unsupported URL" in note


def test_unsupported_retires_when_the_engine_extracts_it():
    check = watch.Check.from_dict(
        {"kind": "unsupported", "engine": "yt-dlp", "url": "https://iwara.ai/video/x"}
    )
    verdict, note = watch.check_unsupported(
        check, "yt-dlp", runner(lambda a: (0, "iwara")),
    )
    assert verdict == watch.RETIRE
    assert "iwara" in note


def test_unsupported_without_the_engine_is_unknown():
    check = watch.Check.from_dict(
        {"kind": "unsupported", "engine": "yt-dlp", "url": "https://iwara.ai/video/x"}
    )
    verdict, _ = watch.check_unsupported(check, None, runner(lambda a: (0, "")))
    assert verdict == watch.UNKNOWN