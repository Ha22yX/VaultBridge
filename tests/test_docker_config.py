from pathlib import Path


def test_dockerfile_defines_nas_runtime_user() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "--uid 1001" in dockerfile
    assert "--gid 1002" in dockerfile
    assert "--home-dir /app/data" in dockerfile


def test_compose_uses_configurable_backup_mount() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "${VAULTBRIDGE_BACKUP_HOST_DIR:-./backups}:/server-backups" in compose
    assert "服务器备份" not in compose
