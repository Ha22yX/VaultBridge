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


def test_build_rsync_shards_batches_root_entries_by_worker_count(monkeypatch, tmp_path: Path) -> None:
    entries = [
        FakeEntry("site-a", stat.S_IFDIR),
        FakeEntry("site-b", stat.S_IFDIR),
        FakeEntry("site-c", stat.S_IFDIR),
        FakeEntry("site-d", stat.S_IFDIR),
        FakeEntry("deploy.sh", stat.S_IFREG),
        FakeEntry("index.html", stat.S_IFREG),
    ]

    def fake_connect_sftp(host: str, port: int, username: str, password: str):
        return FakeSsh(), FakeSftp(entries)

    monkeypatch.setenv("VAULTBRIDGE_RSYNC_WORKERS", "3")
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

    assert len(shards) == 3
    assert all(shard.remote_path == "/www/wwwroot" for shard in shards)
    assert all(shard.source_is_dir for shard in shards)
    assert all(shard.destination == tmp_path for shard in shards)
    assert all(shard.delete for shard in shards)
    assert sorted(item for shard in shards for item in shard.files_from) == [
        "deploy.sh",
        "index.html",
        "site-a/",
        "site-b/",
        "site-c/",
        "site-d/",
    ]
    assert sorted(item for shard in shards for item in shard.expected_children) == [
        "deploy.sh",
        "index.html",
        "site-a",
        "site-b",
        "site-c",
        "site-d",
    ]
