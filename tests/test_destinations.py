from pathlib import Path

from dredge.destinations import filehost_folder, is_porn
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge

FILEHOSTS = ("mega.nz", "mediafire.com")


def knowledge() -> Knowledge:
    return Knowledge(rules={}, global_rule=SiteRule(host="*"), mirrors=MirrorTable())


def test_porn_domain_and_subdomain_match():
    domains = ("spankbang.com", "pornhub.com")
    assert is_porn("https://br.spankbang.com/abc/video", domains)
    assert is_porn("https://www.pornhub.com/view", domains)
    assert not is_porn("https://example.com/x", domains)


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
