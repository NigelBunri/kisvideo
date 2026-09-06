from celery import Celery

from app.config.settings import settings

celery_app = Celery(
    "kis_video",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# Task modules register here as each workstream builds them, e.g.:
# celery_app.autodiscover_tasks(["app.workers"])
