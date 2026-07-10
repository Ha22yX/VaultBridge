from __future__ import annotations

import subprocess
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
