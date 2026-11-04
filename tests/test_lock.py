import os

import pytest

from dredge import cli
from dredge.config import Paths
from dredge.lock import AlreadyRunning, InstanceLock


def test_a_second_lock_is_refused(tmp_path):
    path = tmp_path / "state" / "dredge.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        second = InstanceLock(path)
        with pytest.raises(AlreadyRunning) as caught:
            second.acquire()
        assert caught.value.pid == str(os.getpid())
    finally:
        first.release()


def test_release_allows_another_instance(tmp_path):
    path = tmp_path / "dredge.lock"
    first = InstanceLock(path)
    first.acquire()
    first.release()

    second = InstanceLock(path)
    second.acquire()
    second.release()


def test_a_second_cli_instance_exits_with_3(tmp_path, capsys):
    paths = Paths(tmp_path)
    lock = InstanceLock(paths.lock)
    lock.acquire()
    try:
        code = cli.main(["--root", str(tmp_path), "--once", "--no-prompt"])
    finally:
        lock.release()

    assert code == 3
    assert "another dredge is already running" in capsys.readouterr().out
