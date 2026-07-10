from __future__ import annotations

import os
import posixpath
import subprocess
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import repository
from .rsync_client import RsyncFailed, RsyncProgress, RsyncUnavailable, has_rsync, run_rsync_tree
from .ssh_client import (
    RemoteTarError,
    connect_sftp,
    connect_ssh,
    count_tree,
    download_tree,
    estimate_tree_via_ssh,
    remote_to_snapshot_path,
    should_exclude,
    stream_tar_tree,
)


class BackupError(RuntimeError):
    pass


class BackupStopped(RuntimeError):
    pass


PERFORMANCE_EXCLUDES = [
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.zip",
    "*.7z",
    "*.rar",
    "*.bak",
    "*.dump",
    "*.sql.gz",
]


@dataclass
class RsyncShard:
    remote_path: str
    destination: Path
    source_is_dir: bool
    delete: bool
    label: str


def effective_excludes(job: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for pattern in [*job["exclude_patterns"], *PERFORMANCE_EXCLUDES]:
        cleaned = str(pattern).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            patterns.append(cleaned)
    return patterns


def rsync_worker_count() -> int:
    raw = os.getenv("VAULTBRIDGE_RSYNC_WORKERS", "3")
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return 3


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

    def check_control(self) -> None:
        run = repository.get_run(self.run_id)
        action = run.get("control_action") or "run"
        if action == "stop":
            raise BackupStopped("Backup stopped by user.")
        if action == "pause":
            repository.set_run_status(self.run_id, "paused", "paused", "Backup paused by user.")
            while True:
                time.sleep(1)
                run = repository.get_run(self.run_id)
                action = run.get("control_action") or "run"
                if action == "stop":
                    raise BackupStopped("Backup stopped by user.")
                if action == "run":
                    repository.set_run_status(self.run_id, "running", "syncing", "Backup resumed.")
                    self._last_write = 0
                    return

    def estimate_path(self, path: str) -> None:
        self.check_control()
        self.current_path = path
        self.update(
            phase="estimating",
            message="Estimating remote file count on the server.",
            current_path=path,
        )

    def add_estimate(self, path: str, files_found: int, bytes_found: int) -> None:
        self.check_control()
        self.current_path = path
        self.total_files += files_found
        self.total_bytes += bytes_found
        self.update(
            force=True,
            phase="estimating",
            message=f"Estimated {self.total_files} files before transfer.",
            current_path=path,
            total_files=self.total_files,
            total_bytes=self.total_bytes,
        )

    def scan_path(self, path: str, files_found: int = 0, bytes_found: int = 0) -> None:
        self.check_control()
        self.current_path = path
        self.total_files += files_found
        self.total_bytes += bytes_found
        self.update(
            phase="scanning",
            message=f"Scanning files. {self.total_files} files found.",
            current_path=path,
            total_files=self.total_files,
            total_bytes=self.total_bytes,
        )

    def finish_scan(self) -> None:
        self.update(
            force=True,
            phase="scanning",
            message=f"Scan complete. {self.total_files} files found.",
            total_files=self.total_files,
            total_bytes=self.total_bytes,
            current_path=self.current_path,
        )

    def copied_file(self, path: str, size: int) -> None:
        self.check_control()
        self.current_path = path
        self.copied_files += 1
        self.copied_bytes += size
        if self.total_files:
            message = f"Received {self.copied_files}/{self.total_files} files."
        else:
            message = f"Received {self.copied_files} files."
        self.update(
            phase="syncing",
            message=message,
            current_path=path,
            copied_files=self.copied_files,
            copied_bytes=self.copied_bytes,
        )

    def start_file(self, path: str, size: int) -> None:
        self.check_control()
        self.current_path = path
        if self.total_files:
            message = f"Receiving {self.copied_files + 1}/{self.total_files}: {path}"
        else:
            message = f"Receiving file: {path}"
        self.update(
            force=True,
            phase="syncing",
            message=message,
            current_path=path,
        )

    def copied_chunk(self, path: str, size: int) -> None:
        self.current_path = path
        self.copied_bytes += size
        if self.total_files:
            message = f"Receiving {self.copied_files + 1}/{self.total_files} files."
        else:
            message = f"Received {self.copied_files} files."
        self.update(
            phase="syncing",
            message=message,
            current_path=path,
            copied_files=self.copied_files,
            copied_bytes=self.copied_bytes,
        )

    def flush_copy(self) -> None:
        if self.total_files:
            message = f"Received {self.copied_files}/{self.total_files} files."
        else:
            message = f"Received {self.copied_files} files."
        self.update(
            force=True,
            phase="syncing",
            message=message,
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


def cleanup_stale_files(root: Path, seen_paths: set[Path]) -> None:
    if not root.exists():
        return
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        resolved = path.resolve()
        if resolved == root_resolved or resolved in seen_paths:
            continue
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)


def cleanup_rsync_partials(root: Path) -> None:
    if not root.exists():
        return
    for partial in root.rglob(".rsync-partial"):
        if partial.is_dir():
            shutil.rmtree(partial, ignore_errors=True)


def cleanup_stale_top_level(root: Path, expected_paths: set[Path]) -> None:
    if not root.exists():
        return
    expected = {path.resolve() for path in expected_paths}
    for child in root.iterdir():
        if child.name == ".rsync-partial":
            shutil.rmtree(child, ignore_errors=True)
            continue
        if child.resolve() in expected:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def build_rsync_shards(job: dict[str, Any], remote: str, destination: Path, exclude_patterns: list[str]) -> list[RsyncShard]:
    if rsync_worker_count() <= 1:
        return [RsyncShard(remote, destination, True, True, remote)]

    normalized = remote.rstrip("/")
    ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
    try:
        entries = sftp.listdir_attr(normalized)
    finally:
        sftp.close()
        ssh.close()

    shards: list[RsyncShard] = []
    for attr in sorted(entries, key=lambda item: item.filename.lower()):
        name = attr.filename
        relative = name.replace("\\", "/")
        if should_exclude(name, relative, exclude_patterns):
            continue
        mode = attr.st_mode or 0
        child_remote = posixpath.join(normalized, name)
        if stat.S_ISDIR(mode):
            shards.append(RsyncShard(child_remote, destination / name, True, True, name))
        elif stat.S_ISREG(mode):
            shards.append(RsyncShard(child_remote, destination, False, False, name))

    if len(shards) <= 1:
        return [RsyncShard(remote, destination, True, True, remote)]
    return shards


def _run_sftp_backup(job: dict[str, Any], snapshot: Path, progress: RunProgress, run_id: int) -> tuple[int, int]:
    files = 0
    bytes_written = 0
    exclude_patterns = effective_excludes(job)
    ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
    try:
        repository.update_run_progress(run_id, phase="scanning", message="Scanning remote files with SFTP")
        for remote in job["include_paths"]:
            count_tree(
                sftp,
                remote,
                exclude_patterns,
                progress_callback=progress.scan_path,
            )
        progress.finish_scan()

        repository.update_run_progress(
            run_id,
            phase="syncing",
            message=f"Starting SFTP file copy. {progress.total_files} files found.",
            total_files=progress.total_files,
            total_bytes=progress.total_bytes,
            copied_files=0,
            copied_bytes=0,
        )
        for remote in job["include_paths"]:
            destination = snapshot / remote_to_snapshot_path(remote)
            seen_paths: set[Path] = set()
            current_files, current_bytes = download_tree(
                sftp,
                remote,
                destination,
                exclude_patterns,
                progress_callback=progress.copied_file,
                seen_local_paths=seen_paths,
            )
            cleanup_stale_files(destination, seen_paths)
            files += current_files
            bytes_written += current_bytes
            progress.flush_copy()
    finally:
        sftp.close()
        ssh.close()
    return files, bytes_written


def _run_rsync_backup(job: dict[str, Any], snapshot: Path, progress: RunProgress, run_id: int) -> tuple[int, int]:
    if not has_rsync():
        raise RsyncUnavailable("Local rsync is not installed.")

    files = 0
    bytes_written = 0
    exclude_patterns = effective_excludes(job)
    workers = rsync_worker_count()
    repository.update_run_progress(
        run_id,
        phase="rsyncing",
        message=f"Starting rsync incremental sync with up to {workers} workers. Large archive files are skipped by default.",
        total_files=0,
        copied_files=0,
        total_bytes=0,
        copied_bytes=0,
    )

    for remote in job["include_paths"]:
        progress.check_control()
        destination = snapshot / remote_to_snapshot_path(remote)
        shards = build_rsync_shards(job, remote, destination, exclude_patterns)
        states: dict[int, RsyncProgress] = {}
        state_lock = threading.Lock()

        def make_handler(shard_index: int, shard: RsyncShard):
            def handle_rsync(event: RsyncProgress) -> None:
                display_path = event.current_path or shard.label
                if display_path in {"", "./"}:
                    display_path = shard.label
                elif shard.source_is_dir and not display_path.startswith(shard.label):
                    display_path = f"{shard.label}/{display_path}".replace("//", "/")
                with state_lock:
                    states[shard_index] = event
                    progress.current_path = display_path
                    progress.total_files = sum(item.total_files for item in states.values())
                    progress.copied_files = sum(item.checked_files for item in states.values())
                    progress.copied_bytes = sum(item.transferred_bytes for item in states.values())
                    transferred_files = sum(item.transferred_files for item in states.values())
                    if progress.total_files:
                        message = (
                            f"rsync parallel sync: checked {progress.copied_files}/{progress.total_files} items, "
                            f"transferred {transferred_files} changed files across {min(workers, len(shards))} workers."
                        )
                    elif event.current_file_size:
                        message = f"rsync checking file: {display_path} ({event.current_file_size} bytes)."
                    else:
                        message = f"rsync parallel sync: {display_path}"
                    progress.update(
                        phase="rsyncing",
                        message=message,
                        current_path=progress.current_path,
                        total_files=progress.total_files,
                        copied_files=progress.copied_files,
                        copied_bytes=progress.copied_bytes,
                    )

            return handle_rsync

        expected_paths = {
            shard.destination.resolve()
            if shard.source_is_dir
            else (destination / PurePosixPath(shard.remote_path).name).resolve()
            for shard in shards
        }
        with ThreadPoolExecutor(max_workers=min(workers, len(shards))) as executor:
            futures = [
                executor.submit(
                    run_rsync_tree,
                    host=job["host"],
                    port=job["port"],
                    username=job["username"],
                    password=job["password"],
                    remote_path=shard.remote_path,
                    destination=shard.destination,
                    exclude_patterns=exclude_patterns,
                    source_is_dir=shard.source_is_dir,
                    delete=shard.delete,
                    progress_callback=make_handler(index, shard),
                    control_callback=progress.check_control,
                )
                for index, shard in enumerate(shards)
            ]
            for future in as_completed(futures):
                current_files, current_bytes = future.result()
                files += current_files
                bytes_written += current_bytes
        cleanup_rsync_partials(destination)
        cleanup_stale_top_level(destination, expected_paths)
    progress.update(
        force=True,
        phase="rsyncing",
        message=f"rsync incremental sync complete. Checked {files} items.",
        current_path=progress.current_path,
        total_files=progress.total_files,
        copied_files=progress.copied_files,
        copied_bytes=progress.copied_bytes,
    )
    return files, bytes_written


def _run_tar_backup(job: dict[str, Any], snapshot: Path, progress: RunProgress, run_id: int) -> tuple[int, int]:
    files = 0
    bytes_written = 0
    exclude_patterns = effective_excludes(job)
    ssh = connect_ssh(job["host"], job["port"], job["username"], job["password"])
    try:
        repository.update_run_progress(
            run_id,
            phase="estimating",
            message="Counting files on the server with one SSH command",
            total_files=0,
            copied_files=0,
            total_bytes=0,
            copied_bytes=0,
        )
        for remote in job["include_paths"]:
            progress.estimate_path(remote)
            estimate = estimate_tree_via_ssh(ssh, remote, exclude_patterns)
            if estimate:
                progress.add_estimate(remote, estimate[0], estimate[1])

        repository.update_run_progress(
            run_id,
            phase="syncing",
            message="Fast tar stream started. Receiving files without SFTP recursion.",
            total_files=progress.total_files,
            total_bytes=progress.total_bytes,
            copied_files=0,
            copied_bytes=0,
        )
        for remote in job["include_paths"]:
            destination = snapshot / remote_to_snapshot_path(remote)
            seen_paths: set[Path] = set()
            current_files, current_bytes = stream_tar_tree(
                ssh,
                remote,
                snapshot,
                exclude_patterns,
                progress_callback=progress.copied_file,
                file_start_callback=progress.start_file,
                chunk_callback=progress.copied_chunk,
                control_callback=progress.check_control,
                seen_local_paths=seen_paths,
            )
            cleanup_stale_files(destination, seen_paths)
            files += current_files
            bytes_written += current_bytes
            progress.flush_copy()
    finally:
        ssh.close()
    if not progress.total_files:
        progress.total_files = progress.copied_files
        progress.total_bytes = progress.copied_bytes
        progress.flush_copy()
    return files, bytes_written


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
        try:
            try:
                files, bytes_written = _run_rsync_backup(job, snapshot, progress, run_id)
            except (RsyncUnavailable, RsyncFailed) as exc:
                repository.update_run_progress(
                    run_id,
                    phase="estimating",
                    message=f"rsync unavailable, falling back to tar stream. {exc}",
                    total_files=0,
                    copied_files=0,
                    total_bytes=0,
                    copied_bytes=0,
                )
                progress = RunProgress(run_id)
                files, bytes_written = _run_tar_backup(job, snapshot, progress, run_id)
        except RemoteTarError as exc:
            repository.update_run_progress(
                run_id,
                phase="scanning",
                message=f"Fast tar stream unavailable, falling back to SFTP. {exc}",
                total_files=0,
                copied_files=0,
                total_bytes=0,
                copied_bytes=0,
            )
            progress = RunProgress(run_id)
            files, bytes_written = _run_sftp_backup(job, snapshot, progress, run_id)

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
    except BackupStopped as exc:
        progress.flush_copy()
        repository.finish_run(run_id, "stopped", str(exc), commit_hash)
        return {"run_id": run_id, "files": progress.copied_files, "bytes": progress.copied_bytes, "commit_hash": ""}
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
