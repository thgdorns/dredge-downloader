import threading
from pathlib import Path

from dredge.config import Paths, Runtime, Settings
from dredge.orchestrator import Lane, _free_slot, _take_next
from dredge.sites import MirrorTable, SiteRule, parse_rule
from dredge.urls import Knowledge


def test_zero_lanes_are_clamped_to_one(tmp_path):
    settings = Settings.load(Paths(tmp_path), max_downloads=0)
    runtime = Runtime(Paths(tmp_path), settings)
    assert runtime.max_downloads == 1


def test_free_slot_refuses_a_zero_lane_pool():
    assert _free_slot({}, 0) is None
    assert _free_slot({}, 2) == 1
    assert _free_slot({1: None, 2: None}, 2) is None


def serial_knowledge() -> Knowledge:
    return Knowledge(
        rules={"example.com": parse_rule("example.com serial")},
        global_rule=SiteRule(host="*"),
        mirrors=MirrorTable(),
    )


def lane(url: str) -> Lane:
    return Lane(
        slot=1,
        url=url,
        thread=threading.Thread(),
        result=Path("lane1.result"),
        archive=Path("lane1.archive"),
    )


def test_a_serial_host_is_not_crawled_by_two_lanes():
    urls = ["https://example.com/b", "https://other.example/c"]
    pending = [0, 1]
    active = {1: lane("https://example.com/a")}
    index = _take_next(pending, urls, active, serial_knowledge())
    assert index == 1
    assert pending == [0]


def test_a_non_serial_host_keeps_the_queue_order():
    urls = ["https://example.com/b", "https://other.example/c"]
    pending = [0, 1]
    active = {1: lane("https://third.example/a")}
    index = _take_next(pending, urls, active, serial_knowledge())
    assert index == 0
