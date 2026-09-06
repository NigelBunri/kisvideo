"""Periodic sweep for abandoned tus upload sessions.

An UploadSession that never reaches 'completed' (dropped connection,
crashed client, a user who just never came back) leaves its staging file
on disk forever otherwise - nothing else in this service ever revisits an
'uploading' row once the client stops sending PATCH requests. Runs on
Celery Beat's schedule (see celery_app.py's beat_schedule), not
triggered by any request.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from app.config.settings import settings
from app.db import session_scope
from app.models.upload import UploadSession
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="cleanup_abandoned_uploads")
def cleanup_abandoned_uploads() -> dict:
    """Finds every UploadSession still 'uploading' whose created_at is
    older than settings.upload_ttl_hours, deletes its staging file, and
    marks the row 'expired'. Returns a small summary dict (used by tests
    and useful in worker logs) rather than nothing, since a silent no-op
    return makes "did this actually run and find anything" unanswerable
    from the task result alone.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.upload_ttl_hours)
    swept = 0
    file_missing = 0

    with session_scope() as db:
        expired = (
            db.query(UploadSession)
            .filter(UploadSession.status == "uploading")
            .filter(UploadSession.created_at < cutoff)
            .all()
        )
        for session in expired:
            # A row whose file is already gone (e.g. a previous sweep run
            # crashed after deleting the file but before committing the
            # status update) still needs its status fixed - don't skip
            # the row just because os.remove would raise.
            if os.path.exists(session.staging_path):
                try:
                    os.remove(session.staging_path)
                except OSError:
                    logger.exception("Failed to remove staging file for expired upload %s", session.id)
                    continue
            else:
                file_missing += 1
            session.status = "expired"
            swept += 1

    logger.info("cleanup_abandoned_uploads: swept=%d file_already_missing=%d cutoff=%s", swept, file_missing, cutoff.isoformat())
    return {"swept": swept, "file_already_missing": file_missing}
