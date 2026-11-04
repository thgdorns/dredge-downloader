import sys
from dataclasses import replace
from pathlib import Path

import pytest

from dredge import destinations
from dredge.config import Destination, Paths, Settings
from dredge.destinations import destination_for, filehost_folder
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge

FILEHOSTS = ("mega.nz", "mediafire.com")


def knowledge() -> Knowledge:
    return Knowledge(rules={}, global_rule=SiteRule(host="*"), mirrors=MirrorTable())


def settings_with(destinations=None, default="default", legacy=()):
    return Settings(
        root=Path("."),
        destinations=destinations or {
            "default": Destination("default", Path("Z:/Downloads")),
            "flat": Destination("flat", Path("Z:/OTR/porn"), "flat"),
        },
        default_dest=default,
        legacy_flat=legacy,
    )


def test_a_site_rule_chooses_a_named_destination():
    rules = {"example.com": parse_rule("example.com dest=flat")}
    knowledge_ = Knowledge(rules=rules, global_rule=SiteRule(host="*"), mirrors=MirrorTable())
    target = destination_for("https://example.com/x", knowledge_, settings_with())
    assert (target.name, target.layout) == ("flat", "flat")


def test_an_unknown_destination_falls_back_to_the_default():
    rules = {"example.com": parse_rule("example.com dest=nope")}
    knowledge_ = Knowledge(rules=rules, global_rule=SiteRule(host="*"), mirrors=MirrorTable())
    target = destination_for("https://example.com/x", knowledge_, settings_with())
    assert target.name == "default"


def test_legacy_pornsites_domains_route_to_flat():
    settings = settings_with(legacy=("old.example",))
    knowledge_ = knowledge()
    assert destination_for("https://old.example/x", knowledge_, settings).name == "flat"
    assert destination_for("https://sub.old.example/x", knowledge_, settings).name == "flat"
    assert destination_for("https://other.example/x", knowledge_, settings).name == "default"


def test_folder_link_uses_the_collection_name():
    url = "https://www.mediafire.com/folder/abcd1234/My_Stuff"
    assert filehost_folder(url, knowledge(), FILEHOSTS) == "My_Stuff"


def test_single_file_link_has_no_folder():
    url = "https://www.mediafire.com/file/abc123/file.zip"
    assert filehost_folder(url, knowledge(), FILEHOSTS) == ""


def test_a_host_outside_filehosts_is_not_routed():
    assert filehost_folder("https://example.com/folder/abcd/Name", knowledge(), FILEHOSTS) == ""


def test_folder_rule_in_sites_txt_wins():
    knowledge_ = Knowledge(
        rules={"mediafire.com": parse_rule("mediafire.com folder=/folder/[^/]+/([^/?#]+)")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )
    url = "https://www.mediafire.com/folder/abcd1234/Some%20Name"
    assert filehost_folder(url, knowledge_, FILEHOSTS) == "Some Name"


def test_folder_rule_that_does_not_match_means_single_file():
    knowledge_ = Knowledge(
        rules={"mediafire.com": parse_rule("mediafire.com folder=/folder/[^/]+/([^/?#]+)")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )
    url = "https://www.mediafire.com/file/abc123/file.zip"
    assert filehost_folder(url, knowledge_, FILEHOSTS) == ""


def test_mega_folder_falls_back_to_the_id_without_decryption():
    url = "https://mega.nz/folder/AbCdEf12#KeyPart"
    assert filehost_folder(url, knowledge(), FILEHOSTS) == "AbCdEf12"


def test_a_decrypted_mega_name_is_used_when_given():
    url = "https://mega.nz/folder/AbCdEf12#KeyPart"
    assert filehost_folder(url, knowledge(), FILEHOSTS, mega_name="My Folder") == "My Folder"


def test_path_traversal_in_a_folder_name_is_neutralised():
    url = "https://www.mediafire.com/folder/abcd1234/..%2F..%2Fetc"
    name = filehost_folder(url, knowledge(), FILEHOSTS)
    assert Path(name).name == name


def fallback_settings(tmp_path, **overrides):
    settings = Settings.load(Paths(tmp_path))
    values = {
        "destinations": {
            "default": Destination("default", Path("Z:/Downloads")),
            "flat": Destination("flat", Path("Z:/OTR/porn"), "flat"),
            "music": Destination("music", Path("Y:/Music")),
        },
        "default_dest": "default",
        "dest_default": Path("Z:/Downloads"),
        "dest_flat": Path("Z:/OTR/porn"),
        "dest_fallback": ("D:", "G:"),
        "dest_fallback_below_gb": 20,
        **overrides,
    }
    return replace(settings, **values)


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows concept")
def test_fallback_moves_both_roots_when_the_primary_is_gone(tmp_path, monkeypatch):
    settings = fallback_settings(tmp_path)
    free = {"Z": -1.0, "D": 100.0, "G": 500.0}
    monkeypatch.setattr(destinations, "free_gb", lambda p: free[str(p)[0]])

    out = destinations.apply_fallback(settings)

    assert out.destinations["default"].path == Path("D:/Downloads")
    assert out.destinations["flat"].path == Path("D:/OTR/porn")
    assert out.destinations["music"].path == Path("Y:/Music")
    assert "not mounted" in out.fallback_note


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows concept")
def test_a_full_primary_moves_to_the_first_drive_above_the_bar(tmp_path, monkeypatch):
    settings = fallback_settings(tmp_path)
    free = {"Z": 5.0, "D": 10.0, "G": 500.0}
    monkeypatch.setattr(destinations, "free_gb", lambda p: free[str(p)[0]])

    out = destinations.apply_fallback(settings)

    assert out.destinations["flat"].path == Path("G:/OTR/porn")


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows concept")
def test_nothing_changes_when_the_primary_has_room(tmp_path, monkeypatch):
    settings = fallback_settings(tmp_path)
    monkeypatch.setattr(destinations, "free_gb", lambda p: 500.0)

    assert destinations.apply_fallback(settings) == settings
