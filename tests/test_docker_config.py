from pathlib import Path
import tomllib


def test_dockerfile_defines_nas_runtime_user() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "--uid 1001" in dockerfile
    assert "--gid 1002" in dockerfile
    assert "--home-dir /app/data" in dockerfile


def test_compose_uses_configurable_backup_mount() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "image: ghcr.io/ha22yx/vaultbridge:latest" in compose
    assert "${VAULTBRIDGE_BACKUP_HOST_DIR:-./backups}:/server-backups" in compose
    assert "${VAULTBRIDGE_BACKUP_HOST_DIR:-./backups}:/服务器备份" in compose


def test_runtime_dependencies_are_pinned_for_docker_rebuilds() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert "paramiko==5.0.0" in dependencies
    assert "bcrypt==5.0.0" in dependencies
    assert "pynacl==1.6.2" in dependencies
