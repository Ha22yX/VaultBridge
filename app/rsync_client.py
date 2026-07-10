from __future__ import annotations

import base64
import hashlib
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ssh_client import connect_ssh, normalize_remote_path, parse_host_port


class RsyncUnavailable(RuntimeError):
    pass


class RsyncFailed(RuntimeError):
    pass


@dataclass
class RsyncTools:
    rsync: str
    ssh: str | None = None
    sshpass: str | None = None
    plink: str | None = None
    cygwin_paths: bool = False


@dataclass
class RsyncProgress:
    current_path: str = ""
    current_file_size: int = 0
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


def _existing_file(path: str | Path) -> str | None:
    candidate = Path(path)
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    return None


def _candidate_tool_dirs() -> list[Path]:
    dirs: list[Path] = []
    for name in ("VAULTBRIDGE_CWRSYNC_DIR", "CWRSYNC_HOME", "CWRSYNCHOME"):
        raw = os.getenv(name)
        if raw:
            root = Path(raw)
            dirs.extend([root, root / "bin"])
    if os.name == "nt":
        dirs.extend(
            [
                Path("C:/ProgramData/chocolatey/lib/rsync/tools/bin"),
                Path("C:/cwRsync/bin"),
                Path("C:/cwRsync"),
                Path("C:/Program Files/cwRsync/bin"),
                Path("C:/Program Files/cwRsync"),
                Path("C:/Program Files (x86)/cwRsync/bin"),
                Path("C:/Program Files (x86)/cwRsync"),
                Path("D:/cwRsync/bin"),
                Path("D:/cwRsync"),
                Path("D:/Program Files/cwRsync/bin"),
                Path("D:/Program Files/cwRsync"),
            ]
        )
    seen: set[str] = set()
    result: list[Path] = []
    for directory in dirs:
        key = str(directory).lower()
        if key not in seen:
            seen.add(key)
            result.append(directory)
    return result


def _find_tool_in_dirs(name: str, dirs: list[Path]) -> str | None:
    suffix = ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else ""
    for directory in dirs:
        found = _existing_file(directory / f"{name}{suffix}")
        if found:
            return found
    return shutil.which(name)


def _find_plink() -> str | None:
    candidates = [
        Path("C:/ProgramData/chocolatey/lib/putty.portable/tools/PLINK.EXE"),
        Path("C:/ProgramData/chocolatey/bin/plink.exe"),
        Path("C:/Program Files/PuTTY/plink.exe"),
        Path("C:/Program Files (x86)/PuTTY/plink.exe"),
    ]
    for candidate in candidates:
        found = _existing_file(candidate)
        if found:
            return found
    return shutil.which("plink")


def discover_rsync_tools() -> RsyncTools | None:
    dirs = _candidate_tool_dirs()
    rsync = _find_tool_in_dirs("rsync", dirs)
    if not rsync:
        return None
    rsync_dir = Path(rsync).parent
    ssh = _find_tool_in_dirs("ssh", [rsync_dir, *dirs])
    sshpass = _find_tool_in_dirs("sshpass", [rsync_dir, *dirs])
    plink = _find_plink() if os.name == "nt" else None
    cygwin_paths = os.name == "nt" and rsync.lower().endswith(".exe")
    return RsyncTools(rsync=rsync, ssh=ssh, sshpass=sshpass, plink=plink, cygwin_paths=cygwin_paths)


def has_rsync() -> bool:
    tools = discover_rsync_tools()
    return bool(tools and (tools.ssh or tools.plink))


def _windows_to_cygwin_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    drive = resolved.drive.rstrip(":").lower()
    if drive:
        rest = resolved.as_posix().split(":", 1)[1]
        return f"/cygdrive/{drive}{rest}"
    return resolved.as_posix()


def _rsync_local_path(path: Path, cygwin_paths: bool) -> str:
    value = _windows_to_cygwin_path(path) if cygwin_paths else str(path)
    return value.rstrip("/\\") + "/"


def _shell_path(path: str, cygwin_paths: bool) -> str:
    return _windows_to_cygwin_path(path) if cygwin_paths and re.match(r"^[A-Za-z]:", path) else path


def _windows_program_path(path: str) -> str:
    return path.replace("\\", "/")


def _remote_target(username: str, host: str, remote_path: str, *, include_username: bool = True) -> str:
    clean_host = host
    if ":" in clean_host and not clean_host.startswith("["):
        clean_host = f"[{clean_host}]"
    normalized = normalize_remote_path(remote_path).rstrip("/") + "/"
    prefix = f"{username}@" if include_username else ""
    return f"{prefix}{clean_host}:{normalized}"


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
        size = int(file_match.group("size") or 0)
        return RsyncProgress(current_path=file_match.group("path"), current_file_size=size, message=cleaned)

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


def _write_temp_password_file(password: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    try:
        handle.write(password)
        handle.write("\n")
        return handle.name
    finally:
        handle.close()


def _write_askpass_script() -> str:
    suffix = ".cmd" if os.name == "nt" else ".sh"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False)
    try:
        if os.name == "nt":
            handle.write("@echo off\r\n")
            handle.write(
                "powershell -NoProfile -ExecutionPolicy Bypass "
                "-Command \"[Console]::WriteLine($env:VAULTBRIDGE_RSYNC_PASSWORD)\"\r\n"
            )
        else:
            handle.write("#!/usr/bin/env sh\n")
            handle.write("printf '%s\\n' \"$VAULTBRIDGE_RSYNC_PASSWORD\"\n")
        return handle.name
    finally:
        handle.close()
        if os.name != "nt":
            Path(handle.name).chmod(0o700)


def _remote_hostkey_sha256(host: str, port: int, username: str, password: str) -> str:
    ssh = connect_ssh(host, port, username, password)
    try:
        transport = ssh.get_transport()
        if transport is None:
            raise RsyncUnavailable("Could not read remote SSH host key.")
        key = transport.get_remote_server_key()
        digest = hashlib.sha256(key.asbytes()).digest()
        fingerprint = base64.b64encode(digest).decode("ascii").rstrip("=")
        return f"SHA256:{fingerprint}"
    finally:
        ssh.close()


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
    tools = discover_rsync_tools()
    if not tools:
        raise RsyncUnavailable("Local rsync command is not installed.")

    parsed_host, parsed_port = parse_host_port(host, port)
    env = os.environ.copy()
    env["PATH"] = str(Path(tools.rsync).parent) + os.pathsep + env.get("PATH", "")
    command: list[str] = []
    password_file: str | None = None
    askpass_file: str | None = None
    use_plink = False
    use_askpass = False

    if tools.sshpass and password:
        command.extend([tools.sshpass, "-e"])
        env["SSHPASS"] = password
    elif password and tools.ssh:
        use_askpass = True
        askpass_file = _write_askpass_script()
        env["SSH_ASKPASS"] = askpass_file
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY", ":0")
        env["VAULTBRIDGE_RSYNC_PASSWORD"] = password
    elif os.name == "nt" and tools.plink and password:
        use_plink = True
        password_file = _write_temp_password_file(password)
    elif not tools.ssh:
        raise RsyncUnavailable("Local ssh command is not installed.")

    if use_plink:
        assert tools.plink is not None
        assert password_file is not None
        password_file_arg = _windows_program_path(password_file)
        hostkey = _remote_hostkey_sha256(host, port, username, password)
        ssh_parts = [
            _shell_path(tools.plink, tools.cygwin_paths),
            "-batch",
            "-ssh",
            "-P",
            str(parsed_port),
            "-l",
            username,
            "-pwfile",
            password_file_arg,
            "-hostkey",
            hostkey,
            "-no-antispoof",
        ]
    else:
        assert tools.ssh is not None
        ssh_parts = [
            _shell_path(tools.ssh, tools.cygwin_paths),
            "-p",
            str(parsed_port),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=6",
        ]
        if use_askpass:
            ssh_parts.extend(["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"])
        elif not tools.sshpass:
            ssh_parts.extend(["-o", "BatchMode=yes"])

    destination.mkdir(parents=True, exist_ok=True)
    rsync_args = [
        tools.rsync,
        "-a",
        "--delete",
        "--whole-file",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--omit-dir-times",
        "--partial",
        "--partial-dir=.rsync-partial",
        "--timeout=180",
        "--no-motd",
        "--info=progress2,stats2",
        "--out-format=VB_FILE:%n|%l",
    ]
    if use_plink:
        rsync_args.append("--blocking-io")
    command.extend(
        [
            *rsync_args,
            *_rsync_excludes(exclude_patterns),
            "-e",
            " ".join(shlex_quote(part) for part in ssh_parts),
            _remote_target(username, parsed_host, remote_path, include_username=not use_plink),
            _rsync_local_path(destination, tools.cygwin_paths),
        ]
    )

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        if password_file:
            Path(password_file).unlink(missing_ok=True)
        if askpass_file:
            Path(askpass_file).unlink(missing_ok=True)
        raise RsyncUnavailable(str(exc)) from exc
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
    last_output = time.monotonic()
    forced_success = False
    idle_timeout = int(os.getenv("VAULTBRIDGE_RSYNC_IDLE_TIMEOUT", "180"))
    try:
        while process.poll() is None or not output.empty():
            if control_callback:
                control_callback()
            try:
                line = output.get(timeout=0.25)
            except queue.Empty:
                idle_for = time.monotonic() - last_output
                if (
                    process.poll() is None
                    and last_progress.total_files
                    and last_progress.checked_files >= last_progress.total_files
                    and idle_for > 8
                ):
                    _stop_process(process)
                    forced_success = True
                    break
                if process.poll() is None and idle_for > idle_timeout:
                    _stop_process(process)
                    raise RsyncFailed(f"rsync produced no output for {idle_timeout} seconds.")
                continue
            last_output = time.monotonic()
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
    finally:
        if password_file:
            Path(password_file).unlink(missing_ok=True)
        if askpass_file:
            Path(askpass_file).unlink(missing_ok=True)

    for thread in threads:
        thread.join(timeout=1)

    status = 0 if forced_success else process.returncode
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


def shlex_quote(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_@%+=:,./\\-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
