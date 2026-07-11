from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "VaultBridge"


def data_dir() -> Path:
    raw = os.getenv("VAULTBRIDGE_DATA_DIR", "./data")
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "vaultbridge.sqlite3"


def secret_key_path() -> Path:
    return data_dir() / "secret.key"


def bind_host() -> str:
    return os.getenv("VAULTBRIDGE_HOST", "127.0.0.1")


def bind_port() -> int:
    return int(os.getenv("VAULTBRIDGE_PORT", "8728"))


def archive_retention_hours() -> float:
    return max(0.0, float(os.getenv("VAULTBRIDGE_ARCHIVE_RETENTION_HOURS", "24")))


def archive_partial_retention_hours() -> float:
    return max(0.0, float(os.getenv("VAULTBRIDGE_ARCHIVE_PARTIAL_RETENTION_HOURS", "6")))


def archive_cleanup_interval_minutes() -> int:
    return max(5, int(os.getenv("VAULTBRIDGE_ARCHIVE_CLEANUP_INTERVAL_MINUTES", "60")))
