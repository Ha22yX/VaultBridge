from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .backup import cleanup_archives, run_backup
from .repository import list_jobs
from .settings import archive_cleanup_interval_minutes


scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _job_key(job_id: int) -> str:
    return f"backup-{job_id}"


def _archive_cleanup_key() -> str:
    return "archive-cleanup"


def reload_jobs() -> None:
    for existing in list(scheduler.get_jobs()):
        if existing.id.startswith("backup-"):
            scheduler.remove_job(existing.id)

    for job in list_jobs():
        if not job["enabled"]:
            continue
        trigger_args = {"hour": job["hour"], "minute": job["minute"]}
        if job["schedule_kind"] == "weekly":
            trigger_args["day_of_week"] = str(job["day_of_week"] or 0)
        scheduler.add_job(
            run_backup,
            CronTrigger(**trigger_args),
            id=_job_key(job["id"]),
            args=[job["id"]],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )


def reload_archive_cleanup() -> None:
    scheduler.add_job(
        cleanup_archives,
        IntervalTrigger(minutes=archive_cleanup_interval_minutes()),
        id=_archive_cleanup_key(),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
    reload_jobs()
    reload_archive_cleanup()
    cleanup_archives()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
