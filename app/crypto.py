from __future__ import annotations

import os

from cryptography.fernet import Fernet

from .settings import secret_key_path


def _load_key() -> bytes:
    env_key = os.getenv("BACKUP_SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")

    key_file = secret_key_path()
    if key_file.exists():
        return key_file.read_bytes().strip()

    key = Fernet.generate_key()
    key_file.write_bytes(key)
    return key


_fernet = Fernet(_load_key())


def encrypt_text(value: str) -> str:
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str) -> str:
    return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")

