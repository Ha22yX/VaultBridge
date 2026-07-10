from pathlib import Path

import pytest

from app.ssh_client import normalize_remote_path, parse_host_port, remote_to_snapshot_path, should_exclude


def test_normalize_remote_path() -> None:
    assert normalize_remote_path("www/wwwroot") == "/www/wwwroot"
    assert normalize_remote_path("/www//wwwroot/") == "/www/wwwroot"


def test_remote_to_snapshot_path() -> None:
    assert remote_to_snapshot_path("/www/wwwroot") == Path("www") / "wwwroot"
    with pytest.raises(ValueError):
        remote_to_snapshot_path("/")


def test_excludes() -> None:
    assert should_exclude("node_modules", "site/node_modules", ["node_modules"])
    assert should_exclude("error.log", "logs/error.log", ["*.log"])
    assert not should_exclude("index.php", "site/index.php", ["*.log"])


def test_parse_host_port() -> None:
    assert parse_host_port("147.189.128.208", 22) == ("147.189.128.208", 22)
    assert parse_host_port("147.189.128.208:2222", 22) == ("147.189.128.208", 2222)
    assert parse_host_port("ssh://root@147.189.128.208:2222", 22) == ("147.189.128.208", 2222)
