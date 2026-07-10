from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ssh_client import normalize_remote_path, parse_host_port


class RsyncUnavailable(RuntimeError):
    pass


class RsyncFailed(RuntimeError):
    pass


@dataclass
class RsyncProgress:
    current_path: str = ""
    transferred_bytes: int = 0
    checked_files: int = 0
    total_files: int = 0
    transferred_files: int = 0
    percent: int = 0
    message: str = ""


PROGRESS_RE = re.compile(
    r"(?P<bytes>[0-9,]+)\s+(?P<percent>\d+)%.*?"
    r"\(xfr#(?P<xfr>\d+),\s*(?:to-chk|ir-chk)=(?P<remaining>\d+)/(?P<total>\d+)\)"
)
FILE_RE = re.compile(r"^VB_FILE:(?P<path>.*?)(?:\|(?P<size>\d+))?$")


def has_rsync() -> bool:
    return bool(shutil.which("rsync") and shutil.which("ssh"))


def _remote_target(username: str, host: str, remote_path: str) -> str:
    clean_host = host
    if ":" in clean_host and not clean_host.startswith("["):
        clean_host = f"[{clean_host}]"
    normalized = normalize_remote_path(remote_path).rstrip("/") + "/"
    return f"{username}@{clean_host}:{normalized}"


def _rsync_excludes(patterns: list[str]) -> list[str]:
    args: list[str] = []
    for pattern in patterns:
        cleaned = pattern.strip().replace("\\", "/")
        if cleaned:
            args.extend(["--exclude", cleaned])
    return args


def parse_rsync_progress(line: str, current_path: str = "") -> RsyncProgress | None:
    cleaned = line.strip()
    file_match = FILE_RE.match(cleaned)
    if file_match:
        return RsyncProgress(current_path=file_match.group("path"), message=cleaned)

    match = PROGRESS_RE.search(cleaned)
    if not match:
        return None
    total = int(match.group("total"))
    remaining = int(match.group("remaining"))
    transferred = int(match.group("bytes").replace(",", ""))
    checked = max(0, total - remaining)
    xfr = int(match.group("xfr"))
    percent = int(match.group("percent"))
    return RsyncProgress(
        current_path=current_path,
        transferred_bytes=transferred,
        checked_files=checked,
        total_files=total,
        transferred_files=xfr,
        percent=percent,
        message=cleaned,
    )


def _reader(stream, output: queue.Queue[str], prefix: str) -> None:
    buffer = bytearray()
    try:
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
            value = chunk[0] if isinstance(chunk, bytes) else ord(chunk)
            if value in {10, 13}:
                if buffer:
                    output.put(f"{prefix}{buffer.decode('utf-8', errors='replace')}")
                    buffer.clear()
                continue
            buffer.append(value)
        if buffer:
            output.put(f"{prefix}{buffer.decode('utf-8', errors='replace')}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def run_rsync_tree(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    remote_path: str,
    destination: Path,
    exclude_patterns: list[str],
    progress_callback: Callable[[RsyncProgress], None] | None = None,
    control_callback: Callable[[], None] | None = None,
) -> tuple[int, int]:
    rsync_path = shutil.which("rsync")
    ssh_path = shutil.which("ssh")
    if not rsync_path or not ssh_path:
        raise RsyncUnavailable("Local rsync or ssh command is not installed.")

    parsed_host, parsed_port = parse_host_port(host, port)
    sshpass_path = shutil.which("sshpass")
    env = os.environ.copy()
    command: list[str] = []
    if sshpass_path and password:
        command.extend([sshpass_path, "-e"])
        env["SSHPASS"] = password

    ssh_parts = [
        ssh_path,
        "-p",
        str(parsed_port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=6",
    ]
    if not sshpass_path:
        ssh_parts.extend(["-o", "BatchMode=yes"])

    destination.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            rsync_path,
            "-a",
            "--delete",
            "--partial",
            "--partial-dir=.rsync-partial",
            "--no-motd",
            "--info=progress2,stats2",
            "--out-format=VB_FILE:%n|%l",
            *_rsync_excludes(exclude_patterns),
            "-e",
            " ".join(ssh_parts),
            _remote_target(username, parsed_host, remote_path),
            str(destination) + os.sep,
        ]
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    output: queue.Queue[str] = queue.Queue()
    threads = [
        threading.Thread(target=_reader, args=(process.stdout, output, ""), daemon=True),
        threading.Thread(target=_reader, args=(process.stderr, output, "ERR:"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    current_path = normalize_remote_path(remote_path)
    last_progress = RsyncProgress(current_path=current_path)
    stderr_lines: list[str] = []
    try:
        while process.poll() is None or not output.empty():
            if control_callback:
                control_callback()
            try:
                line = output.get(timeout=0.25)
            except queue.Empty:
                continue
            if line.startswith("ERR:"):
                stderr_lines.append(line.removeprefix("ERR:"))
                continue
            progress = parse_rsync_progress(line, current_path)
            if not progress:
                continue
            if progress.current_path:
                current_path = progress.current_path
            if not progress.current_path:
                progress.current_path = current_path
            last_progress = progress
            if progress_callback:
                progress_callback(progress)
    except Exception:
        _stop_process(process)
        raise

    for thread in threads:
        thread.join(timeout=1)

    status = process.returncode
    if status not in {0, 23, 24}:
        detail = "\n".join(stderr_lines[-5:]).strip()
        raise RsyncFailed(detail or f"rsync exited with status {status}")
    if status in {23, 24} and stderr_lines:
        last_progress.message = stderr_lines[-1]

    # Rsync status 23/24 can happen when live website files change during transfer.
    # The completed files are still usable, and the next run will reconcile changes.
    if progress_callback:
        progress_callback(last_progress)
    return (last_progress.checked_files or last_progress.transferred_files, last_progress.transferred_bytes)
