from app.rsync_client import parse_rsync_progress


def test_parse_rsync_file_line() -> None:
    progress = parse_rsync_progress("VB_FILE:www/wwwroot/index.php|128")

    assert progress is not None
    assert progress.current_path == "www/wwwroot/index.php"


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
