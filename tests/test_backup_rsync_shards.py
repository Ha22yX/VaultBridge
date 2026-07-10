import stat
from pathlib import Path

from app import backup


class FakeSftp:
    def __init__(self, entries):
        self.entries = entries

    def listdir_attr(self, path: str):
        assert path == "/www/wwwroot"
        return self.entries

    def close(self) -> None:
        pass


class FakeSsh:
    def close(self) -> None:
        pass


class FakeEntry:
    def __init__(self, filename: str, mode: int) -> None:
        self.filename = filename
        self.st_mode = mode


def test_build_rsync_shards_groups_root_files(monkeypatch, tmp_path: Path) -> None:
    entries = [
        FakeEntry("site-a", stat.S_IFDIR),
        FakeEntry("deploy.sh", stat.S_IFREG),
        FakeEntry("index.html", stat.S_IFREG),
    ]

    def fake_connect_sftp(host: str, port: int, username: str, password: str):
        return FakeSsh(), FakeSftp(entries)

    monkeypatch.setenv("VAULTBRIDGE_RSYNC_WORKERS", "2")
    monkeypatch.setattr(backup, "connect_sftp", fake_connect_sftp)

    shards = backup.build_rsync_shards(
        {
            "host": "example.com",
            "port": 22,
            "username": "root",
            "password": "secret",
        },
        "/www/wwwroot",
        tmp_path,
        [],
    )

    assert len(shards) == 2
    assert shards[0].root_files_only
    assert shards[0].expected_children == ("deploy.sh", "index.html")
    assert shards[0].destination == tmp_path
    assert shards[1].remote_path == "/www/wwwroot/site-a"
    assert shards[1].destination == tmp_path / "site-a"
