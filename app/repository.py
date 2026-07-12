from __future__ import annotations

import json
import sqlite3
from typing import Any

from .crypto import decrypt_text, encrypt_text
from .db import connect, row_to_dict
from .schemas import JobIn, JobPatch


def _decode_job(row: sqlite3.Row | None, include_password: bool = False) -> dict[str, Any] | None:
    job = row_to_dict(row)
    if job is None:
        return None
    job["include_paths"] = json.loads(job["include_paths"])
    job["exclude_patterns"] = json.loads(job["exclude_patterns"])
    job["enabled"] = bool(job["enabled"])
    if include_password:
        job["password"] = decrypt_text(job["password_enc"])
    job.pop("password_enc", None)
    return job


def list_jobs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    return [_decode_job(row) for row in rows if row is not None]


def get_job(job_id: int, include_password: bool = False) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    job = _decode_job(row, include_password=include_password)
    if job is None:
        raise KeyError(f"Job {job_id} not found")
    return job


def create_job(payload: JobIn) -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (
              name, host, port, username, password_enc, target_path,
              include_paths, exclude_patterns, schedule_kind, day_of_week,
              hour, minute, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.host,
                payload.port,
                payload.username,
                encrypt_text(payload.password),
                payload.target_path,
                json.dumps(payload.include_paths, ensure_ascii=False),
                json.dumps(payload.exclude_patterns, ensure_ascii=False),
                payload.schedule_kind,
                payload.day_of_week if payload.schedule_kind == "weekly" else None,
                payload.hour,
                payload.minute,
                int(payload.enabled),
            ),
        )
        job_id = cursor.lastrowid
    return get_job(int(job_id))


def update_job(job_id: int, patch: JobPatch) -> dict[str, Any]:
    current = get_job(job_id, include_password=True)
    data = patch.model_dump(exclude_unset=True)
    current.update({k: v for k, v in data.items() if v is not None})
    password = current.pop("password")
    if patch.password:
        password = patch.password
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs SET
              name = ?, host = ?, port = ?, username = ?, password_enc = ?,
              target_path = ?, include_paths = ?, exclude_patterns = ?,
              schedule_kind = ?, day_of_week = ?, hour = ?, minute = ?,
              enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                current["name"],
                current["host"],
                current["port"],
                current["username"],
                encrypt_text(password),
                current["target_path"],
                json.dumps(current["include_paths"], ensure_ascii=False),
                json.dumps(current["exclude_patterns"], ensure_ascii=False),
                current["schedule_kind"],
                current["day_of_week"] if current["schedule_kind"] == "weekly" else None,
                current["hour"],
                current["minute"],
                int(current["enabled"]),
                job_id,
            ),
        )
    return get_job(job_id)


def delete_job(job_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def create_run(job_id: int, status: str, message: str | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (job_id, status, message, phase, control_action) VALUES (?, ?, ?, ?, ?)",
            (job_id, status, message, "starting", "run"),
        )
        return int(cursor.lastrowid)


def update_run_progress(
    run_id: int,
    *,
    phase: str | None = None,
    message: str | None = None,
    current_path: str | None = None,
    total_files: int | None = None,
    copied_files: int | None = None,
    total_bytes: int | None = None,
    copied_bytes: int | None = None,
) -> None:
    updates: list[str] = []
    values: list[Any] = []
    fields = {
        "phase": phase,
        "message": message,
        "current_path": current_path,
        "total_files": total_files,
        "copied_files": copied_files,
        "total_bytes": total_bytes,
        "copied_bytes": copied_bytes,
    }
    for field, value in fields.items():
        if value is not None:
            updates.append(f"{field} = ?")
            values.append(value)
    if not updates:
        return
    values.append(run_id)
    with connect() as conn:
        conn.execute(f"UPDATE runs SET {', '.join(updates)} WHERE id = ?", values)


def get_run(run_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    run = row_to_dict(row)
    if run is None:
        raise KeyError(f"Run {run_id} not found")
    return run


def set_run_control(run_id: int, action: str) -> dict[str, Any]:
    if action not in {"run", "pause", "stop"}:
        raise ValueError("Invalid run control action")
    with connect() as conn:
        conn.execute("UPDATE runs SET control_action = ? WHERE id = ?", (action, run_id))
    return get_run(run_id)


def set_run_status(run_id: int, status: str, phase: str, message: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, phase = ?, message = COALESCE(?, message) WHERE id = ?",
            (status, phase, message, run_id),
        )


def finish_run(run_id: int, status: str, message: str | None = None, commit_hash: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = ?, phase = ?, control_action = 'run', message = ?, commit_hash = ?, finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, status, message, commit_hash, run_id),
        )


def delete_run(run_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))


def list_runs(job_id: int | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if job_id:
            rows = conn.execute(
                "SELECT * FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT 50", (job_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def get_version_metadata(job_id: int, commit_hash: str) -> dict[str, int] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT file_count, total_bytes
            FROM version_metadata
            WHERE job_id = ? AND commit_hash = ?
            """,
            (job_id, commit_hash),
        ).fetchone()
    if row is None:
        return None
    return {"file_count": int(row["file_count"]), "total_bytes": int(row["total_bytes"])}


def list_version_metadata(job_id: int, commit_hashes: list[str]) -> dict[str, dict[str, int]]:
    if not commit_hashes:
        return {}
    placeholders = ", ".join("?" for _ in commit_hashes)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT commit_hash, file_count, total_bytes
            FROM version_metadata
            WHERE job_id = ? AND commit_hash IN ({placeholders})
            """,
            [job_id, *commit_hashes],
        ).fetchall()
    return {
        row["commit_hash"]: {"file_count": int(row["file_count"]), "total_bytes": int(row["total_bytes"])}
        for row in rows
    }


def upsert_version_metadata(job_id: int, commit_hash: str, file_count: int, total_bytes: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO version_metadata (job_id, commit_hash, file_count, total_bytes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, commit_hash) DO UPDATE SET
              file_count = excluded.file_count,
              total_bytes = excluded.total_bytes,
              updated_at = CURRENT_TIMESTAMP
            """,
            (job_id, commit_hash, file_count, total_bytes),
        )
