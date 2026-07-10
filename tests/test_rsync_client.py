from pathlib import Path

from app.rsync_client import (
    _remote_target,
    _rsync_base_args,
    _rsync_local_path,
    _windows_to_cygwin_path,
    parse_rsync_progress,
)


def test_parse_rsync_file_line() -> None:
    progress = parse_rsync_progress("VB_FILE:www/wwwroot/index.php|128")

    assert progress is not None
    assert progress.current_path == "www/wwwroot/index.php"
    assert progress.current_file_size == 128


def test_parse_rsync_progress_line() -> None:
    progress = parse_rsync_progress(
        "      1,048,576  12%   18.44MB/s    0:00:01 (xfr#3, to-chk=21/42)",
        current_path="www/wwwroot/app.bin",
    )

    assert progress is not None
    assert progress.current_path == "www/wwwroot/app.bin"
    assert progress.transferred_bytes == 1048576
    assert progress.checked_files == 21
    assert progress.total_files == 42
    assert progress.transferred_files == 3
    assert progress.percent == 12


def test_parse_rsync_incremental_check_line() -> None:
    progress = parse_rsync_progress(
        "        262,144   3%    6.10MB/s    0:00:00 (xfr#1, ir-chk=9/12)",
        current_path="www/wwwroot/photo.jpg",
    )

    assert progress is not None
    assert progress.checked_files == 3
    assert progress.total_files == 12


def test_remote_target_can_omit_username() -> None:
    assert _remote_target("root", "example.com", "/www/wwwroot") == "root@example.com:/www/wwwroot/"
    assert (
        _remote_target("root", "example.com", "/www/wwwroot", include_username=False)
        == "example.com:/www/wwwroot/"
    )
    assert (
        _remote_target("root", "example.com", "/www/wwwroot/file.txt", source_is_dir=False)
        == "root@example.com:/www/wwwroot/file.txt"
    )


def test_windows_to_cygwin_path() -> None:
    converted = _windows_to_cygwin_path(Path("C:/Users/Administrator/Desktop/backup"))

    assert converted.lower().startswith("/cygdrive/c/")
    assert converted.endswith("/Users/Administrator/Desktop/backup")


def test_rsync_local_path_trailing_slash() -> None:
    assert _rsync_local_path(Path("C:/backup"), cygwin_paths=True).endswith("/")


def test_files_from_rsync_args_keep_directory_recursion() -> None:
    args = _rsync_base_args(root_files_only=False, files_from=True)

    assert "-a" in args
    assert "-r" in args
    assert args.index("-r") > args.index("-a")
