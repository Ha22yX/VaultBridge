from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import repository
from .backup import create_archive, list_versions, run_backup
from .db import init_db
from .schemas import BrowseIn, ConnectionIn, JobIn, JobPatch
from .scheduler import reload_jobs, start_scheduler, stop_scheduler
from .settings import APP_NAME, bind_host, bind_port
from .ssh_client import connect_sftp, list_remote, normalize_remote_path


app = FastAPI(title=APP_NAME)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_scheduler()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": APP_NAME}


@app.get("/api/jobs")
def api_list_jobs() -> list[dict]:
    return repository.list_jobs()


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: int) -> dict:
    try:
        return repository.get_job(job_id, include_password=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs")
def api_create_job(payload: JobIn) -> dict:
    job = repository.create_job(payload)
    reload_jobs()
    return job


@app.patch("/api/jobs/{job_id}")
def api_update_job(job_id: int, payload: JobPatch) -> dict:
    try:
        job = repository.update_job(job_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    reload_jobs()
    return job


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: int) -> dict[str, str]:
    repository.delete_job(job_id)
    reload_jobs()
    return {"status": "deleted"}


@app.post("/api/ssh/test")
def api_test_connection(payload: ConnectionIn) -> dict[str, str]:
    try:
        ssh, sftp = connect_sftp(payload.host, payload.port, payload.username, payload.password)
        try:
            return {"status": "ok", "cwd": sftp.getcwd() or "/"}
        finally:
            sftp.close()
            ssh.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ssh/browse")
def api_browse_connection(payload: BrowseIn) -> list[dict]:
    try:
        ssh, sftp = connect_sftp(payload.host, payload.port, payload.username, payload.password)
        try:
            return [entry.__dict__ for entry in list_remote(sftp, normalize_remote_path(payload.path))]
        finally:
            sftp.close()
            ssh.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/test")
def api_test_job(job_id: int) -> dict[str, str]:
    try:
        job = repository.get_job(job_id, include_password=True)
        ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
        try:
            return {"status": "ok", "cwd": sftp.getcwd() or "/"}
        finally:
            sftp.close()
            ssh.close()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/browse")
def api_browse(job_id: int, path: str = Query(default="/")) -> list[dict]:
    try:
        job = repository.get_job(job_id, include_password=True)
        ssh, sftp = connect_sftp(job["host"], job["port"], job["username"], job["password"])
        try:
            return [entry.__dict__ for entry in list_remote(sftp, normalize_remote_path(path))]
        finally:
            sftp.close()
            ssh.close()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/run")
def api_run_now(job_id: int, background_tasks: BackgroundTasks) -> dict[str, str]:
    try:
        repository.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(run_backup, job_id)
    return {"status": "queued"}


@app.get("/api/jobs/{job_id}/versions")
def api_versions(job_id: int) -> list[dict[str, str]]:
    try:
        return list_versions(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/versions/{commit}/download")
def api_download_version(job_id: int, commit: str) -> FileResponse:
    try:
        archive = create_archive(job_id, commit)
        return FileResponse(archive, filename=archive.name, media_type="application/zip")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runs")
def api_runs(job_id: int | None = None) -> list[dict]:
    return repository.list_runs(job_id)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=bind_host(), port=bind_port(), reload=False)
