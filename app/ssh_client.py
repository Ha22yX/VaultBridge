from __future__ import annotations

import fnmatch
import os
import posixpath
import shlex
import shutil
import socket
import stat
import tarfile
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


class RemoteTarError(RuntimeError):
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


def connect_ssh(host: str, port: int, username: str, password: str) -> paramiko.SSHClient:
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
        return client
    except Exception as exc:
        client.close()
        raise friendly_ssh_error(exc, host, port) from exc


def connect_sftp(host: str, port: int, username: str, password: str) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    client = connect_ssh(host, port, username, password)
    try:
        return client, client.open_sftp()
    except Exception:
        client.close()
        raise


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


def _tar_exclude_args(patterns: list[str]) -> list[str]:
    args: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        cleaned = pattern.strip().replace("\\", "/")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidates = [cleaned]
        if "/" not in cleaned and not cleaned.startswith("*"):
            candidates.append(f"*/{cleaned}")
        for candidate in candidates:
            args.append(f"--exclude={shlex.quote(candidate)}")
    return args


def _find_prune_expr(patterns: list[str]) -> str:
    terms: list[str] = []
    for pattern in patterns:
        cleaned = pattern.strip().replace("\\", "/")
        if not cleaned:
            continue
        operator = "-path" if "/" in cleaned else "-name"
        value = f"*/{cleaned}" if operator == "-path" and not cleaned.startswith("*") else cleaned
        terms.append(f"{operator} {shlex.quote(value)}")
    if not terms:
        return ""
    return "\\( " + " -o ".join(terms) + " \\) -prune -o "


def _tar_relative_path(remote_path: str) -> str:
    normalized = normalize_remote_path(remote_path)
    remote_to_snapshot_path(normalized)
    return normalized.lstrip("/")


def estimate_tree_via_ssh(
    ssh: paramiko.SSHClient,
    remote_path: str,
    exclude_patterns: list[str],
) -> tuple[int, int] | None:
    normalized = normalize_remote_path(remote_path)
    prune_expr = _find_prune_expr(exclude_patterns)
    command = (
        f"find {shlex.quote(normalized)} {prune_expr}-type f -printf '%s\\n' 2>/dev/null "
        "| awk '{count += 1; bytes += $1} END {printf \"%d %d\\n\", count, bytes}'"
    )
    _, stdout, _ = ssh.exec_command(command, get_pty=False)
    output = stdout.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    parts = output.strip().split()
    if status != 0 or len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return (int(parts[0]), int(parts[1]))


def _safe_member_path(snapshot_root: Path, member_name: str) -> Path | None:
    pure = PurePosixPath(member_name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    root = snapshot_root.resolve()
    target = (root / Path(*pure.parts)).resolve()
    if not target.is_relative_to(root):
        return None
    return target


def stream_tar_tree(
    ssh: paramiko.SSHClient,
    remote_path: str,
    snapshot_root: Path,
    exclude_patterns: list[str],
    *,
    progress_callback: Callable[[str, int], None] | None = None,
    control_callback: Callable[[], None] | None = None,
    seen_local_paths: set[Path] | None = None,
) -> tuple[int, int]:
    normalized = normalize_remote_path(remote_path)
    relative = _tar_relative_path(normalized)
    root_member = PurePosixPath(relative)
    exclude_args = " ".join(_tar_exclude_args(exclude_patterns))
    command_parts = ["tar", "-C", "/", "-cf", "-"]
    if exclude_args:
        command_parts.append(exclude_args)
    command_parts.append(shlex.quote(relative))
    command = " ".join(command_parts)

    _, stdout, stderr = ssh.exec_command(command, get_pty=False)
    files = 0
    bytes_seen = 0
    channel = stdout.channel
    try:
        with tarfile.open(fileobj=stdout, mode="r|*") as archive:
            for member in archive:
                if control_callback:
                    control_callback()
                target = _safe_member_path(snapshot_root, member.name)
                if target is None:
                    continue

                parts = PurePosixPath(member.name).parts
                if tuple(parts[: len(root_member.parts)]) != tuple(root_member.parts):
                    continue
                relative_to_root = posixpath.relpath(member.name, relative)
                if relative_to_root == ".":
                    relative_to_root = ""
                if should_exclude(posixpath.basename(member.name), relative_to_root, exclude_patterns):
                    continue

                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    if seen_local_paths is not None:
                        seen_local_paths.add(target.resolve())
                    continue
                if not member.isfile():
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                size = int(member.size or 0)
                if seen_local_paths is not None:
                    seen_local_paths.add(target.resolve())

                should_write = True
                if target.exists() and target.is_file():
                    stat_result = target.stat()
                    should_write = not (
                        stat_result.st_size == size and int(stat_result.st_mtime) == int(member.mtime)
                    )

                source = archive.extractfile(member)
                if source is not None:
                    if should_write:
                        with target.open("wb") as handle:
                            shutil.copyfileobj(source, handle, length=1024 * 1024)
                        os.utime(target, (int(member.mtime), int(member.mtime)))
                    else:
                        with open(os.devnull, "wb") as devnull:
                            shutil.copyfileobj(source, devnull, length=1024 * 1024)

                files += 1
                bytes_seen += size
                if progress_callback:
                    progress_callback(normalized + "/" + relative_to_root if relative_to_root else normalized, size)
    except tarfile.TarError as exc:
        channel.close()
        error_text = stderr.read().decode("utf-8", errors="replace").strip()
        raise RemoteTarError(error_text or str(exc)) from exc
    except Exception:
        channel.close()
        raise

    status = channel.recv_exit_status()
    error_text = stderr.read().decode("utf-8", errors="replace").strip()
    if status not in {0, 1}:
        raise RemoteTarError(error_text or f"Remote tar exited with status {status}")
    return (files, bytes_seen)


def count_tree(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    exclude_patterns: list[str],
    root_remote: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[int, int]:
    root_remote = normalize_remote_path(root_remote or remote_path)
    remote_path = normalize_remote_path(remote_path)
    attr = sftp.lstat(remote_path)
    mode = attr.st_mode or 0

    if progress_callback:
        progress_callback(remote_path, 0, 0)

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

    size = int(attr.st_size or 0)
    if progress_callback:
        progress_callback(remote_path, 1, size)
    return (1, size)


def download_tree(
    sftp: paramiko.SFTPClient,
    remote_path: str,
    local_path: Path,
    exclude_patterns: list[str],
    root_remote: str | None = None,
    progress_callback: Callable[[str, int], None] | None = None,
    seen_local_paths: set[Path] | None = None,
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
        if seen_local_paths is not None:
            seen_local_paths.add(local_path.resolve())
        files = 0
        bytes_written = 0
        for child in sftp.listdir_attr(remote_path):
            child_remote = posixpath.join(remote_path, child.filename)
            child_local = local_path / child.filename
            child_files, child_bytes = download_tree(
                sftp,
                child_remote,
                child_local,
                exclude_patterns,
                root_remote,
                progress_callback,
                seen_local_paths,
            )
            files += child_files
            bytes_written += child_bytes
        return (files, bytes_written)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    size = int(attr.st_size or 0)
    if seen_local_paths is not None:
        seen_local_paths.add(local_path.resolve())
    if not (local_path.exists() and local_path.is_file() and local_path.stat().st_size == size):
        sftp.get(remote_path, str(local_path))
    if progress_callback:
        progress_callback(remote_path, size)
    return (1, size)
