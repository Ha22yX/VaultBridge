from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import repository
from .ssh_client import connect_sftp, count_tree, download_tree, remote_to_snapshot_path


class BackupError(RuntimeError):
    pass


class RunProgress:
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.total_files = 0
        self.total_bytes = 0
        self.copied_files = 0
        self.copied_bytes = 0
        self.current_path = ""
        self._last_write = 0.0

    def update(self, *, force: bool = False, **fields: Any) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        repository.update_run_progress(self.run_id, **fields)

    def scan_path(self, path: str) -> None:
        self.current_path = path
        self.update(phase="scanning", message=f"Scanning {path}", current_path=path)

    def add_totals(self, files: int, bytes_total: int) -> None:
        self.total_files += files
        self.total_bytes += bytes_total
        self.update(
            force=True,
            phase="scanning",
            message=f"Scan complete. {self.total_files} files found.",
            total_files=self.total_files,
            total_bytes=self.total_bytes,
            current_path=self.current_path,
        )

    def copied_file(self, path: str, size: int) -> None:
        self.current_path = path
        self.copied_files += 1
        self.copied_bytes += size
        self.update(
            phase="syncing",
            message=f"Copied {self.copied_files}/{self.total_files} files.",
            current_path=path,
            copied_files=self.copied_files,
            copied_bytes=self.copied_bytes,
        )

    def flush_copy(self) -> None:
        self.update(
            force=True,
            phase="syncing",
            message=f"Copied {self.copied_files}/{self.total_files} files.",
            current_path=self.current_path,
            copied_files=self.copied_files,
            copied_bytes=self.copied_bytes,
        )


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise BackupError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
    return result


def repo_root(job: dict[str, Any]) -> Path:
    return Path(job["target_path"]).expanduser().resolve() / "repository"


def archives_root(job: dict[str, Any]) -> Path:
    return Path(job["target_path"]).expanduser().resolve() / "archives"


def ensure_repo(job: dict[str, Any]) -> Path:
    root = repo_root(job)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _run_git(["init"], root)
        _run_git(["config", "user.name", "VaultBridge"], root)
        _run_git(["config", "user.email", "vaultbridge@local"], root)
    return root


def run_backup(job_id: int) -> dict[str, str | int]:
    job = repository.get_job(job_id, include_password=True)
    run_id = repository.create_run(job_id, "running", "Backup started")
    progress = RunProgress(run_id)
    files = 0
    bytes_written = 0
    commit_hash: str | None = None

    try:
        repository.update_run_progress(run_id, phase="preparing", message="Preparing local Git repository")
        root = ensure_repo(job)
        snapshot = root / "snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)

        repository.update_run_progress(run_id, phase="connecting", message="Connecting to SSH server")
        ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
        try:
            repository.update_run_progress(run_id, phase="scanning", message="Scanning remote files")
            for remote in job["include_paths"]:
                current_files, current_bytes = count_tree(
                    sftp,
                    remote,
                    job["exclude_patterns"],
                    progress_callback=progress.scan_path,
                )
                progress.add_totals(current_files, current_bytes)

            repository.update_run_progress(
                run_id,
                phase="syncing",
                message=f"Starting file copy. {progress.total_files} files found.",
                total_files=progress.total_files,
                total_bytes=progress.total_bytes,
                copied_files=0,
                copied_bytes=0,
            )
            for remote in job["include_paths"]:
                destination = snapshot / remote_to_snapshot_path(remote)
                if destination.exists():
                    shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
                current_files, current_bytes = download_tree(
                    sftp,
                    remote,
                    destination,
                    job["exclude_patterns"],
                    progress_callback=progress.copied_file,
                )
                files += current_files
                bytes_written += current_bytes
                progress.flush_copy()
        finally:
            sftp.close()
            ssh.close()

        repository.update_run_progress(
            run_id,
            phase="committing",
            message="Writing Git commit",
            copied_files=progress.copied_files,
            copied_bytes=progress.copied_bytes,
            current_path=progress.current_path,
        )
        _run_git(["add", "snapshot"], root)
        status = _run_git(["status", "--porcelain"], root).stdout.strip()
        if status:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _run_git(["commit", "-m", f"Backup {stamp}"], root)
            commit_hash = _run_git(["rev-parse", "HEAD"], root).stdout.strip()
            message = f"Completed. {files} files, {bytes_written} bytes, commit {commit_hash[:8]}."
            repository.finish_run(run_id, "success", message, commit_hash)
        else:
            current = _run_git(["rev-parse", "--verify", "HEAD"], root, check=False)
            commit_hash = current.stdout.strip() if current.returncode == 0 else None
            message = f"No changes. Checked {files} files, {bytes_written} bytes."
            repository.finish_run(run_id, "success", message, commit_hash)
        return {"run_id": run_id, "files": files, "bytes": bytes_written, "commit_hash": commit_hash or ""}
    except Exception as exc:
        repository.finish_run(run_id, "failed", str(exc), commit_hash)
        raise


def list_versions(job_id: int) -> list[dict[str, str]]:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        return []
    result = _run_git(
        ["log", "--pretty=format:%H%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M:%S", "--max-count=100"],
        root,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    versions = []
    for line in result.stdout.splitlines():
        commit, date, subject = line.split("\t", 2)
        versions.append({"commit": commit, "date": date, "subject": subject})
    return versions


def create_archive(job_id: int, commit: str) -> Path:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        raise BackupError("No Git repository exists for this job yet")
    safe_commit = "".join(char for char in commit if char.isalnum())[:40]
    if len(safe_commit) < 7:
        raise BackupError("Invalid commit")
    archives = archives_root(job)
    archives.mkdir(parents=True, exist_ok=True)
    output = archives / f"vaultbridge-{safe_commit[:12]}.zip"
    _run_git(["archive", "--format=zip", f"--output={output}", safe_commit, "snapshot"], root)
    return output
