from __future__ import annotations

import fnmatch
import posixpath
import socket
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    is_symlink: bool
    size: int
    mtime: int


class SSHConnectionError(RuntimeError):
    pass


def parse_host_port(host: str, port: int) -> tuple[str, int]:
    cleaned = host.strip()
    if cleaned.startswith("ssh://"):
        cleaned = cleaned.removeprefix("ssh://")
    if "@" in cleaned:
        cleaned = cleaned.rsplit("@", 1)[1]
    if cleaned.startswith("[") and "]:" in cleaned:
        host_part, port_part = cleaned.rsplit("]:", 1)
        return host_part.removeprefix("["), int(port_part)
    if cleaned.count(":") == 1:
        host_part, port_part = cleaned.rsplit(":", 1)
        if port_part.isdigit():
            return host_part, int(port_part)
    return cleaned, port


def friendly_ssh_error(exc: Exception, host: str, port: int) -> SSHConnectionError:
    target = f"{host}:{port}"
    message = str(exc)
    if isinstance(exc, paramiko.AuthenticationException):
        return SSHConnectionError(f"SSH 认证失败：请检查用户名、密码，以及服务器是否允许该用户密码登录。目标：{target}")
    if isinstance(exc, paramiko.BadHostKeyException):
        return SSHConnectionError(f"SSH 主机密钥校验失败。目标：{target}")
    if isinstance(exc, NoValidConnectionsError):
        return SSHConnectionError(f"无法连接到 SSH 端口：请检查 IP、端口、安全组和防火墙。目标：{target}")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SSHConnectionError(f"SSH 连接超时：端口可能被防火墙拦截，或服务器响应太慢。目标：{target}")
    if isinstance(exc, paramiko.SSHException) and "Error reading SSH protocol banner" in message:
        return SSHConnectionError(
            f"端口没有返回 SSH 握手信息：请确认填写的是 SSH 端口，不是宝塔面板、网站或其他服务端口。目标：{target}"
        )
    if isinstance(exc, paramiko.SSHException):
        return SSHConnectionError(f"SSH 连接失败：{message}。目标：{target}")
    return SSHConnectionError(f"连接失败：{message}。目标：{target}")


def connect_sftp(host: str, port: int, username: str, password: str) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    host, port = parse_host_port(host, port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=20,
            banner_timeout=60,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        return client, client.open_sftp()
    except Exception as exc:
        client.close()
        raise friendly_ssh_error(exc, host, port) from exc


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


def count_tree(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    exclude_patterns: list[str],
    root_remote: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    root_remote = normalize_remote_path(root_remote or remote_path)
    remote_path = normalize_remote_path(remote_path)
    attr = sftp.lstat(remote_path)
    mode = attr.st_mode or 0

    if progress_callback:
        progress_callback(remote_path)

    if stat.S_ISLNK(mode):
        return (0, 0)

    relative = posixpath.relpath(remote_path, root_remote)
    if relative == ".":
        relative = ""
    if should_exclude(posixpath.basename(remote_path), relative, exclude_patterns):
        return (0, 0)

    if stat.S_ISDIR(mode):
        files = 0
        bytes_total = 0
        for child in sftp.listdir_attr(remote_path):
            child_remote = posixpath.join(remote_path, child.filename)
            child_files, child_bytes = count_tree(
                sftp, child_remote, exclude_patterns, root_remote, progress_callback
            )
            files += child_files
            bytes_total += child_bytes
        return (files, bytes_total)

    return (1, int(attr.st_size or 0))


def download_tree(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    local_path: Path,
    exclude_patterns: list[str],
    root_remote: str | None = None,
    progress_callback: Callable[[str, int], None] | None = None,
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
                sftp, child_remote, child_local, exclude_patterns, root_remote, progress_callback
            )
            files += child_files
            bytes_written += child_bytes
        return (files, bytes_written)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    sftp.get(remote_path, str(local_path))
    size = int(attr.st_size or 0)
    if progress_callback:
        progress_callback(remote_path, size)
    return (1, size)
