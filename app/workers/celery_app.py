from celery import Celery

from app.config.settings import settings

celery_app = Celery(
    "kis_video",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# app.workers.transcode imports celery_app from THIS module (see its own
# import) to register transcode_video via the @celery_app.task decorator —
# importing it here (for its side effect, not any name used below) is what
# actually makes that task known to this app when a worker process starts.
# Deliberately a plain import, not autodiscover_tasks(["app.workers"]):
# autodiscover would also import ffmpeg_utils.py/other non-task modules
# under app.workers/ for no benefit, and silently swallow an import error
# in any of them (autodiscover logs a warning and moves on) rather than
# failing worker startup loudly the way a normal import does — a transcode
# task that silently failed to register would show up as "jobs stay queued
# forever" with no error anywhere obvious, exactly the kind of silent
# failure this whole service is trying to avoid.
import app.workers.transcode  # noqa: E402,F401
import app.workers.cleanup  # noqa: E402,F401

# Runs on a Celery Beat process (a separate `celery -A app.workers.celery_app
# beat` command/container - beat only schedules, it doesn't execute; a
# regular worker process still has to be running to actually pick the task
# up). Hourly is frequent enough that upload_ttl_hours' cutoff is enforced
# within about an hour of actually crossing it, not frequent enough to be
# meaningfully expensive (a single indexed query over 'uploading' rows).
celery_app.conf.beat_schedule = {
    "cleanup-abandoned-uploads-hourly": {
        "task": "cleanup_abandoned_uploads",
        "schedule": 3600.0,
    },
}
