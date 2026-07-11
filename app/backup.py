from __future__ import annotations

import os
import subprocess
import shutil
import stat
import threading
import time
import uuid
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


ARCHIVE_PROGRESS: dict[str, dict[str, Any]] = {}
ARCHIVE_PROGRESS_LOCK = threading.Lock()


PERFORMANCE_EXCLUDES = [
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "site-packages",
    "*.pyc",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.zip",
    "*.7z",
    "*.rar",
    "*.bak",
    "*.dump",
    "*.sql.gz",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.db-shm",
    "*.db-wal",
]


@dataclass
class RsyncShard:
    remote_path: str
    destination: Path
    source_is_dir: bool
    delete: bool
    label: str
    root_files_only: bool = False
    expected_children: tuple[str, ...] = ()
    files_from: tuple[str, ...] = ()


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


def rsync_retry_count() -> int:
    raw = os.getenv("VAULTBRIDGE_RSYNC_RETRIES", "2")
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def rsync_resume_retry_count() -> int:
    raw = os.getenv("VAULTBRIDGE_RSYNC_RESUME_RETRIES", "10")
    try:
        return max(0, min(50, int(raw)))
    except ValueError:
        return 10


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
    result.stdout = result.stdout or ""
    result.stderr = result.stderr or ""
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
    _run_git(["config", "core.autocrlf", "false"], root)
    _run_git(["config", "core.safecrlf", "false"], root)
    _run_git(["config", "core.longpaths", "true"], root)
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


def cleanup_windows_reparse_points(root: Path) -> None:
    if os.name != "nt" or not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if not (path.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                continue
        except (AttributeError, OSError):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)


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
    entries = None
    for attempt in range(3):
        try:
            ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
            try:
                entries = sftp.listdir_attr(normalized)
                break
            finally:
                sftp.close()
                ssh.close()
        except Exception:
            if attempt == 2:
                return [RsyncShard(remote, destination, True, True, remote)]
            time.sleep(1 + attempt)
    if entries is None:
        return [RsyncShard(remote, destination, True, True, remote)]

    items: list[tuple[str, str]] = []
    for attr in sorted(entries, key=lambda item: item.filename.lower()):
        name = attr.filename
        relative = name.replace("\\", "/")
        if should_exclude(name, relative, exclude_patterns):
            continue
        mode = attr.st_mode or 0
        if stat.S_ISDIR(mode):
            items.append((f"{name}/", name))
        elif stat.S_ISREG(mode):
            items.append((name, name))

    if len(items) <= 1:
        return [RsyncShard(remote, destination, True, True, remote)]

    batch_count = min(rsync_worker_count(), len(items))
    batches: list[list[str]] = [[] for _ in range(batch_count)]
    expected_batches: list[list[str]] = [[] for _ in range(batch_count)]
    for index, (files_from_entry, expected_name) in enumerate(items):
        batch_index = index % batch_count
        batches[batch_index].append(files_from_entry)
        expected_batches[batch_index].append(expected_name)

    shards = []
    for index, batch in enumerate(batches):
        preview = ", ".join(entry.rstrip("/") for entry in batch[:3])
        if len(batch) > 3:
            preview += f" +{len(batch) - 3}"
        shards.append(
            RsyncShard(
                remote,
                destination,
                True,
                True,
                f"batch {index + 1}/{batch_count}: {preview}",
                expected_children=tuple(expected_batches[index]),
                files_from=tuple(batch),
            )
        )
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
        cleanup_windows_reparse_points(destination)
        shards = build_rsync_shards(job, remote, destination, exclude_patterns)
        states: dict[int, RsyncProgress] = {}
        state_lock = threading.Lock()

        def make_handler(shard_index: int, shard: RsyncShard):
            def handle_rsync(event: RsyncProgress) -> None:
                display_path = event.current_path or shard.label
                if display_path in {"", "./"}:
                    display_path = shard.label
                elif shard.source_is_dir and not shard.files_from and not display_path.startswith(shard.label):
                    display_path = f"{shard.label}/{display_path}".replace("//", "/")
                with state_lock:
                    previous = states.get(shard_index, RsyncProgress())
                    states[shard_index] = RsyncProgress(
                        current_path=display_path,
                        current_file_size=event.current_file_size or previous.current_file_size,
                        transferred_bytes=max(previous.transferred_bytes, event.transferred_bytes),
                        checked_files=max(previous.checked_files, event.checked_files),
                        total_files=max(previous.total_files, event.total_files),
                        transferred_files=max(previous.transferred_files, event.transferred_files),
                        percent=max(previous.percent, event.percent),
                        message=event.message or previous.message,
                    )
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

        expected_paths: set[Path] = set()
        for shard in shards:
            if shard.files_from:
                expected_paths.update((destination / name).resolve() for name in shard.expected_children)
            elif shard.root_files_only:
                expected_paths.update((destination / name).resolve() for name in shard.expected_children)
            elif shard.source_is_dir:
                expected_paths.add(shard.destination.resolve())
            else:
                expected_paths.add((destination / PurePosixPath(shard.remote_path).name).resolve())
        with ThreadPoolExecutor(max_workers=min(workers, len(shards))) as executor:
            def run_shard(index: int, shard: RsyncShard) -> tuple[int, int]:
                if index:
                    time.sleep(min(index * 2, 8))
                retries = rsync_retry_count()
                for attempt in range(retries + 1):
                    try:
                        return run_rsync_tree(
                            host=job["host"],
                            port=job["port"],
                            username=job["username"],
                            password=job["password"],
                            remote_path=shard.remote_path,
                            destination=shard.destination,
                            exclude_patterns=exclude_patterns,
                            source_is_dir=shard.source_is_dir,
                            delete=shard.delete,
                            root_files_only=shard.root_files_only,
                            files_from=shard.files_from,
                            progress_callback=make_handler(index, shard),
                            control_callback=progress.check_control,
                        )
                    except RsyncFailed:
                        if attempt >= retries:
                            raise
                        progress.update(
                            force=True,
                            phase="rsyncing",
                            message=f"rsync connection dropped on {shard.label}; retrying {attempt + 1}/{retries}.",
                            current_path=shard.label,
                            total_files=progress.total_files,
                            copied_files=progress.copied_files,
                            copied_bytes=progress.copied_bytes,
                        )
                        time.sleep(3 + attempt * 3)
                return (0, 0)

            futures = [
                executor.submit(
                    run_shard,
                    index,
                    shard,
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


def _run_rsync_backup_with_resume(
    job: dict[str, Any],
    snapshot: Path,
    progress: RunProgress,
    run_id: int,
) -> tuple[int, int]:
    retries = rsync_resume_retry_count()
    for attempt in range(retries + 1):
        try:
            return _run_rsync_backup(job, snapshot, progress, run_id)
        except RsyncFailed as exc:
            if attempt >= retries:
                raise
            retry_number = attempt + 1
            delay = min(5 + attempt * 5, 45)
            progress.update(
                force=True,
                phase="rsyncing",
                message=(
                    f"rsync 连接中断，正在自动继续 {retry_number}/{retries}。"
                    "已同步和 partial 文件会保留，下一轮会继续增量同步。"
                ),
                current_path=progress.current_path,
                total_files=progress.total_files,
                copied_files=progress.copied_files,
                copied_bytes=progress.copied_bytes,
            )
            for _ in range(delay):
                if hasattr(progress, "check_control"):
                    progress.check_control()
                time.sleep(1)
    raise RsyncFailed("rsync failed after automatic resume retries")


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
                files, bytes_written = _run_rsync_backup_with_resume(job, snapshot, progress, run_id)
            except RsyncUnavailable as exc:
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
            except RsyncFailed as exc:
                raise BackupError(
                    "rsync 多次自动继续后仍然失败。Partial 文件已保留，下一次任务仍会继续增量同步。"
                    f"{exc}"
                ) from exc
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


def _safe_commit_ref(commit: str) -> str:
    safe_commit = "".join(char for char in commit if char.isalnum())[:40]
    if len(safe_commit) < 7:
        raise BackupError("Invalid commit")
    return safe_commit


def _normalize_version_path(path: str) -> str:
    cleaned = (path or "").replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    parts = PurePosixPath(cleaned).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupError("Invalid version path")
    return "/".join(parts)


def _snapshot_treeish(commit: str, path: str = "") -> str:
    safe_commit = _safe_commit_ref(commit)
    normalized = _normalize_version_path(path)
    if normalized:
        return f"{safe_commit}:snapshot/{normalized}"
    return f"{safe_commit}:snapshot"


def _version_snapshot_summary(root: Path, commit: str) -> dict[str, int]:
    result = _run_git(["ls-tree", "-r", "-l", _safe_commit_ref(commit), "snapshot"], root, check=False)
    if result.returncode != 0:
        return {"file_count": 0, "total_bytes": 0}
    file_count = 0
    total_bytes = 0
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        metadata, _path = line.split("\t", 1)
        fields = metadata.split()
        if len(fields) < 4 or fields[1] != "blob":
            continue
        file_count += 1
        if fields[3].isdigit():
            total_bytes += int(fields[3])
    return {"file_count": file_count, "total_bytes": total_bytes}


def _version_log_entry(root: Path, commit: str) -> dict[str, Any]:
    result = _run_git(
        ["log", "-1", "--pretty=format:%H%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M:%S", _safe_commit_ref(commit)],
        root,
    )
    full_commit, date, subject = result.stdout.strip().split("\t", 2)
    return {
        "commit": full_commit,
        "date": date,
        "subject": subject,
        **_version_snapshot_summary(root, full_commit),
    }


def list_versions(job_id: int) -> list[dict[str, Any]]:
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
        versions.append(
            {
                "commit": commit,
                "date": date,
                "subject": subject,
                **_version_snapshot_summary(root, commit),
            }
        )
    return versions


def get_version_detail(job_id: int, commit: str) -> dict[str, Any]:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        raise BackupError("No Git repository exists for this job yet")
    return _version_log_entry(root, commit)


def list_version_tree(job_id: int, commit: str, path: str = "") -> dict[str, Any]:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        raise BackupError("No Git repository exists for this job yet")
    normalized = _normalize_version_path(path)
    result = _run_git(["ls-tree", "-l", _snapshot_treeish(commit, normalized)], root, check=False)
    if result.returncode != 0:
        raise BackupError(result.stderr.strip() or "Version path not found")
    entries = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        metadata, entry_name = line.split("\t", 1)
        fields = metadata.split()
        if len(fields) < 4:
            continue
        kind = "dir" if fields[1] == "tree" else "file"
        size = int(fields[3]) if kind == "file" and fields[3].isdigit() else None
        entry_path = "/".join(part for part in [normalized, entry_name] if part)
        entries.append({"name": entry_name, "path": entry_path, "type": kind, "size": size})
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    parent = None
    if normalized:
        parent_parts = normalized.split("/")[:-1]
        parent = "/".join(parent_parts) if parent_parts else ""
    return {"commit": _safe_commit_ref(commit), "path": normalized, "parent": parent, "entries": entries}


def create_archive(job_id: int, commit: str) -> Path:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        raise BackupError("No Git repository exists for this job yet")
    safe_commit = _safe_commit_ref(commit)
    archives = archives_root(job)
    archives.mkdir(parents=True, exist_ok=True)
    output = archives / f"vaultbridge-{safe_commit[:12]}.zip"
    _run_git(["archive", "--format=zip", f"--output={output}", safe_commit, "snapshot"], root)
    return output


def _archive_progress_snapshot(task_id: str) -> dict[str, Any]:
    with ARCHIVE_PROGRESS_LOCK:
        task = ARCHIVE_PROGRESS.get(task_id)
        if not task:
            raise BackupError("Archive task not found")
        return dict(task)


def get_archive_task(task_id: str) -> dict[str, Any]:
    return _archive_progress_snapshot(task_id)


def _set_archive_progress(task_id: str, **fields: Any) -> None:
    with ARCHIVE_PROGRESS_LOCK:
        task = ARCHIVE_PROGRESS.get(task_id)
        if not task:
            return
        task.update(fields)
        task["updated_at"] = time.time()


def _run_archive_task(task_id: str, job_id: int, commit: str) -> None:
    job = repository.get_job(job_id)
    root = repo_root(job)
    safe_commit = _safe_commit_ref(commit)
    archives = archives_root(job)
    archives.mkdir(parents=True, exist_ok=True)
    output = archives / f"vaultbridge-{safe_commit[:12]}.zip"
    partial = archives / f".vaultbridge-{safe_commit[:12]}-{task_id}.zip.part"
    summary = _version_snapshot_summary(root, safe_commit)
    total_bytes = int(summary.get("total_bytes") or 0)
    total_files = int(summary.get("file_count") or 0)

    _set_archive_progress(
        task_id,
        status="running",
        phase="compressing",
        message=f"Compressing {total_files} files into a zip archive.",
        percent=2,
        total_bytes=total_bytes,
        total_files=total_files,
        written_bytes=0,
    )

    partial.unlink(missing_ok=True)
    process = subprocess.Popen(
        ["git", "archive", "--format=zip", f"--output={partial}", safe_commit, "snapshot"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        while process.poll() is None:
            written = partial.stat().st_size if partial.exists() else 0
            if total_bytes > 0:
                percent = min(95, max(2, int((written / total_bytes) * 100)))
            else:
                percent = 50
            _set_archive_progress(
                task_id,
                percent=percent,
                written_bytes=written,
                message=f"Compressing zip archive: {format_bytes_for_message(written)} written.",
            )
            time.sleep(0.5)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            partial.unlink(missing_ok=True)
            raise BackupError((stderr or stdout or "Git archive failed").strip())
        partial.replace(output)
        written = output.stat().st_size if output.exists() else 0
        _set_archive_progress(
            task_id,
            status="ready",
            phase="ready",
            message=f"Zip archive is ready: {format_bytes_for_message(written)}.",
            percent=100,
            written_bytes=written,
            archive_path=str(output),
            download_url=f"/api/archive-tasks/{task_id}/download",
        )
    except Exception as exc:
        partial.unlink(missing_ok=True)
        _set_archive_progress(
            task_id,
            status="failed",
            phase="failed",
            message=str(exc),
            percent=100,
        )


def format_bytes_for_message(bytes_value: int) -> str:
    value = float(bytes_value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"


def start_archive_task(job_id: int, commit: str) -> dict[str, Any]:
    job = repository.get_job(job_id)
    root = repo_root(job)
    if not (root / ".git").exists():
        raise BackupError("No Git repository exists for this job yet")
    safe_commit = _safe_commit_ref(commit)
    existing = archives_root(job) / f"vaultbridge-{safe_commit[:12]}.zip"
    task_id = uuid.uuid4().hex
    with ARCHIVE_PROGRESS_LOCK:
        ARCHIVE_PROGRESS[task_id] = {
            "id": task_id,
            "job_id": job_id,
            "commit": safe_commit,
            "status": "ready" if existing.exists() else "queued",
            "phase": "ready" if existing.exists() else "queued",
            "message": "Zip archive is already ready." if existing.exists() else "Preparing zip archive.",
            "percent": 100 if existing.exists() else 0,
            "total_bytes": 0,
            "total_files": 0,
            "written_bytes": existing.stat().st_size if existing.exists() else 0,
            "archive_path": str(existing) if existing.exists() else "",
            "download_url": f"/api/archive-tasks/{task_id}/download" if existing.exists() else "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    if not existing.exists():
        thread = threading.Thread(target=_run_archive_task, args=(task_id, job_id, safe_commit), daemon=True)
        thread.start()
    return _archive_progress_snapshot(task_id)


def archive_task_file(task_id: str) -> Path:
    task = _archive_progress_snapshot(task_id)
    if task.get("status") != "ready" or not task.get("archive_path"):
        raise BackupError("Archive is not ready yet")
    path = Path(str(task["archive_path"]))
    if not path.exists():
        raise BackupError("Archive file no longer exists")
    return path
