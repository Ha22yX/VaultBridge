import os
import stat
from pathlib import Path

from app.backup import prepare_snapshot_for_git


def test_prepare_snapshot_for_git_makes_owner_readable(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    target = snapshot / "www" / "wwwroot" / "site" / "frontend" / "let"
    target.parent.mkdir(parents=True)
    target.write_text("asset", encoding="utf-8")
    os.chmod(target, stat.S_IXUSR)

    prepare_snapshot_for_git(snapshot)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & stat.S_IRUSR
    assert mode & stat.S_IWUSR
