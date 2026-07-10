from __future__ import annotations

import fnmatch
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import paramiko


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    is_symlink: bool
    size: int
    mtime: int


def connect_sftp(host: str, port: int, username: str, password: str) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client, client.open_sftp()


def normalize_remote_path(path: str) -> str:
    if not path:
        return "/"
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def remote_to_snapshot_path(remote_path: str) -> Path:
    normalized = normalize_remote_path(remote_path)
    pure = PurePosixPath(normalized)
    parts = [part for part in pure.parts if part not in {"/", "", ".", ".."}]
    if not parts:
        raise ValueError("Refusing to back up the filesystem root directly")
    return Path(*parts)


def list_remote(sftp: paramiko.SFTPClient, path: str) -> list[RemoteEntry]:
    normalized = normalize_remote_path(path)
    entries: list[RemoteEntry] = []
    for attr in sftp.listdir_attr(normalized):
        mode = attr.st_mode or 0
        child = posixpath.join(normalized, attr.filename)
        entries.append(
            RemoteEntry(
                name=attr.filename,
                path=child,
                is_dir=stat.S_ISDIR(mode),
                is_symlink=stat.S_ISLNK(mode),
                size=int(attr.st_size or 0),
                mtime=int(attr.st_mtime or 0),
            )
        )
    entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
    return entries


def should_exclude(name: str, relative_path: str, patterns: list[str]) -> bool:
    normalized = relative_path.replace("\\", "/")
    for pattern in patterns:
        cleaned = pattern.strip()
        if not cleaned:
            continue
        if fnmatch.fnmatch(name, cleaned) or fnmatch.fnmatch(normalized, cleaned):
            return True
    return False


def download_tree(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    local_path: Path,
    exclude_patterns: list[str],
    root_remote: str | None = None,
) -> tuple[int, int]:
    root_remote = normalize_remote_path(root_remote or remote_path)
    remote_path = normalize_remote_path(remote_path)
    attr = sftp.lstat(remote_path)
    mode = attr.st_mode or 0

    if stat.S_ISLNK(mode):
        return (0, 0)

    relative = posixpath.relpath(remote_path, root_remote)
    if relative == ".":
        relative = ""
    if should_exclude(posixpath.basename(remote_path), relative, exclude_patterns):
        return (0, 0)

    if stat.S_ISDIR(mode):
        local_path.mkdir(parents=True, exist_ok=True)
        files = 0
        bytes_written = 0
        for child in sftp.listdir_attr(remote_path):
            child_remote = posixpath.join(remote_path, child.filename)
            child_local = local_path / child.filename
            child_files, child_bytes = download_tree(
                sftp, child_remote, child_local, exclude_patterns, root_remote
            )
            files += child_files
            bytes_written += child_bytes
        return (files, bytes_written)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    sftp.get(remote_path, str(local_path))
    return (1, int(attr.st_size or 0))

