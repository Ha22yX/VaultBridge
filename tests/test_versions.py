from __future__ import annotations

import subprocess
import os
import time
from pathlib import Path

from app import backup


INDEX_HTML = "hello"
APP_JS = "console.log('ok');"
README_TXT = "notes"


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _create_repo(tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "backup-target"
    root = target / "repository"
    snapshot = root / "snapshot"
    (snapshot / "www" / "site-a" / "assets").mkdir(parents=True)
    (snapshot / "www" / "site-a" / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (snapshot / "www" / "site-a" / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")
    (snapshot / "www" / "site-b").mkdir(parents=True)
    (snapshot / "www" / "site-b" / "readme.txt").write_text(README_TXT, encoding="utf-8")
    _git(["init"], root)
    _git(["config", "user.name", "VaultBridge Test"], root)
    _git(["config", "user.email", "vaultbridge-test@example.local"], root)
    _git(["add", "snapshot"], root)
    _git(["commit", "-m", "Backup test"], root)
    commit = _git(["rev-parse", "HEAD"], root)
    return target, commit


def test_list_versions_includes_snapshot_size(monkeypatch, tmp_path: Path) -> None:
    target, commit = _create_repo(tmp_path)
    monkeypatch.setattr(backup.repository, "get_job", lambda job_id: {"target_path": str(target)})

    versions = backup.list_versions(1)

    assert versions == [
        {
            "commit": commit,
            "date": versions[0]["date"],
            "subject": "Backup test",
            "file_count": 3,
            "total_bytes": len(INDEX_HTML) + len(APP_JS) + len(README_TXT),
        }
    ]


def test_version_tree_browses_commit_snapshot(monkeypatch, tmp_path: Path) -> None:
    target, commit = _create_repo(tmp_path)
    monkeypatch.setattr(backup.repository, "get_job", lambda job_id: {"target_path": str(target)})

    root_tree = backup.list_version_tree(1, commit, "")
    site_tree = backup.list_version_tree(1, commit, "www/site-a")

    assert root_tree["path"] == ""
    assert root_tree["parent"] is None
    assert root_tree["entries"] == [{"name": "www", "path": "www", "type": "dir", "size": None}]
    assert site_tree["path"] == "www/site-a"
    assert site_tree["parent"] == "www"
    assert site_tree["entries"] == [
        {"name": "assets", "path": "www/site-a/assets", "type": "dir", "size": None},
        {"name": "index.html", "path": "www/site-a/index.html", "type": "file", "size": len(INDEX_HTML)},
    ]


def test_cleanup_archives_removes_expired_zip_and_part(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "backup-target"
    archives = target / "archives"
    archives.mkdir(parents=True)
    old_zip = archives / "vaultbridge-old.zip"
    new_zip = archives / "vaultbridge-new.zip"
    old_part = archives / ".vaultbridge-old.zip.part"
    ignored = archives / "notes.txt"
    for path in [old_zip, new_zip, old_part, ignored]:
        path.write_text("data", encoding="utf-8")
    now = time.time()
    old_mtime = now - 3 * 3600
    for path in [old_zip, old_part]:
        os.utime(path, (old_mtime, old_mtime))
    monkeypatch.setattr(backup.repository, "list_jobs", lambda: [{"target_path": str(target)}])
    monkeypatch.setattr(backup, "archive_retention_hours", lambda: 1)
    monkeypatch.setattr(backup, "archive_partial_retention_hours", lambda: 1)

    result = backup.cleanup_archives(now=now)

    assert result["deleted"] == 2
    assert not old_zip.exists()
    assert not old_part.exists()
    assert new_zip.exists()
    assert ignored.exists()
