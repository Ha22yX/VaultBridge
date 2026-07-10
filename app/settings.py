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

