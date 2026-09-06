"""The transcoding pipeline's single entry point: transcode_video(job_id).

ARCHITECTURE.md lists this as a 6-step "Celery task chain" (probe ->
renditions -> master playlist -> thumbnail -> S3 upload -> notify). This is
implemented as ONE Celery task calling through those steps sequentially,
not as five separate chained tasks — a job either produces a complete,
coherent set of renditions or it doesn't; there's no meaningful "partially
succeeded" state for an Asset (a master playlist referencing a rendition
that never finished transcoding is simply broken). A literal Celery
chain() would need to pass state between tasks via the result backend and
compose per-task retry/failure semantics into one "mark the whole job
failed and clean up" outcome anyway — a single task with ordinary
sequential Python calls does the same thing with far less machinery, and
still calls into per-step functions (probe/transcode_rendition/
write_master_playlist/create_thumbnail/upload_file are all separately
testable), which is what actually matters for "chain" here: separable
steps, not necessarily separate Celery task boundaries.

Coordinate with dev-5b (the upload API): call
`transcode_video.delay(job_id)` immediately after creating a TranscodeJob
row for a completed UploadSession — this task does not create that row
itself, only consumes it.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
import json as json_module
from datetime import datetime, timezone

from app.db import session_scope
from app.models.asset import Asset, TranscodeJob
from app.models.upload import UploadSession
from app.storage.s3_client import S3UploadError, upload_directory, upload_file
from app.workers.celery_app import celery_app
from app.workers.ffmpeg_utils import (
    FfmpegError,
    create_thumbnail,
    probe,
    renditions_for_source,
    transcode_rendition,
    write_master_playlist,
)

logger = logging.getLogger(__name__)


def _mark_failed(job_id: str, error_message: str) -> None:
    """Isolated in its own session_scope — called from the outer except
    block of transcode_video, where the *original* session_scope may
    itself be the thing that failed/rolled back. A fresh session here
    guarantees the failure is actually recorded even if the main
    transaction is unusable."""
    logger.error("TranscodeJob %s failed: %s", job_id, error_message)
    try:
        with session_scope() as db:
            job = db.get(TranscodeJob, job_id)
            if not job:
                return
            job.status = "failed"
            job.error_message = error_message[:2048]
            job.completed_at = datetime.now(timezone.utc)
            callback_url = job.callback_url
            caller_reference = job.caller_reference
        if callback_url:
            _send_webhook(callback_url, {
                "job_id": job_id,
                "caller_reference": caller_reference,
                "status": "failed",
                "error_message": error_message[:2048],
            })
    except Exception:
        # This function IS the last line of defense against a silently
        # stuck job — if even this fails, log loudly rather than raise
        # again and lose the original error in a new traceback.
        logger.exception("Failed to record failure for TranscodeJob %s — job may be stuck as 'transcoding'.", job_id)


def _send_webhook(url: str, payload: dict) -> None:
    """Plain urllib POST — no new HTTP-client dependency for a single JSON
    POST (requests/httpx aren't in requirements.txt and adding one for
    this alone isn't worth the coordination overhead on a shared file).
    Best-effort: a failed webhook delivery is logged, never re-raised —
    the job's own status is already durably recorded in Postgres by the
    time this is called, and GET /jobs/{id} polling is the documented
    fallback per ARCHITECTURE.md, so a dropped webhook is degraded, not
    silently lost."""
    body = json_module.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                logger.warning("Webhook to %s returned status %s", url, resp.status)
    except urllib.error.URLError as exc:
        logger.warning("Webhook to %s failed: %s", url, exc)


@celery_app.task(name="transcode_video", bind=True, max_retries=0)
def transcode_video(self, job_id: str) -> None:
    work_dir = tempfile.mkdtemp(prefix=f"kisvideo-{job_id}-")
    try:
        with session_scope() as db:
            job = db.get(TranscodeJob, job_id)
            if not job:
                logger.error("transcode_video called with unknown job_id=%s", job_id)
                return
            upload = db.get(UploadSession, job.upload_session_id)
            if not upload or not os.path.exists(upload.staging_path):
                raise FileNotFoundError(
                    f"Staged source file missing for job {job_id} "
                    f"(upload_session_id={job.upload_session_id})"
                )
            source_path = upload.staging_path
            callback_url = job.callback_url
            caller_reference = job.caller_reference
            job.status = "probing"

        # ── 1. Probe ─────────────────────────────────────────────────────
        result = probe(source_path)
        with session_scope() as db:
            job = db.get(TranscodeJob, job_id)
            job.source_duration_seconds = result.duration_seconds
            job.source_width = result.width
            job.source_height = result.height
            job.status = "transcoding"

        # ── 2. Transcode renditions (only <= source resolution) ─────────
        renditions = renditions_for_source(result.height)
        rendition_dir = os.path.join(work_dir, "renditions")
        for rendition in renditions:
            transcode_rendition(source_path, rendition_dir, rendition)

        # ── 3. Master playlist ───────────────────────────────────────────
        write_master_playlist(rendition_dir, renditions)

        # ── 4. Thumbnail ──────────────────────────────────────────────────
        thumbnail_path = os.path.join(work_dir, "thumbnail.jpg")
        has_thumbnail = create_thumbnail(source_path, thumbnail_path)

        # ── 5. Upload everything to S3 ────────────────────────────────────
        key_prefix = f"videos/{job_id}"
        rendition_urls = upload_directory(rendition_dir, f"{key_prefix}/renditions")
        master_playlist_url = rendition_urls.get("master.m3u8")
        if not master_playlist_url:
            raise FfmpegError("master.m3u8 was not produced/uploaded — cannot finalize this job")

        thumbnail_url = None
        if has_thumbnail:
            thumbnail_url = upload_file(thumbnail_path, f"{key_prefix}/thumbnail.jpg")

        # ── 6. Finalize: create Asset, mark ready, notify ────────────────
        # renditions_payload (the per-height {height, bitrate_kbps,
        # playlist_url} list) still gets stored on Asset.renditions itself
        # for admin/debug visibility (its own model doc comment), but is
        # deliberately NOT part of the webhook payload below — coordinated
        # directly with dev-83 (jobs/playback API): the webhook's "asset"
        # object is meant to be the exact same AssetSummary shape their
        # GET /jobs/{id} already returns inline, so anyone consuming either
        # the push (webhook) or poll (GET) path gets one consistent
        # contract. Anyone who genuinely needs the full rendition ladder
        # can still call GET /assets/{id} directly.
        renditions_payload = [
            {
                "height": r.height,
                "bitrate_kbps": r.bitrate_kbps,
                "playlist_url": rendition_urls.get(r.playlist_filename),
            }
            for r in renditions
        ]
        # Generated explicitly (not left to Asset.id's own Python-side
        # `default=`) so it's a known value here, usable in the webhook
        # payload below without depending on SQLAlchemy's flush timing to
        # have populated it back onto the ORM object first.
        asset_id = str(uuid.uuid4())
        with session_scope() as db:
            job = db.get(TranscodeJob, job_id)
            job.status = "ready"
            job.completed_at = datetime.now(timezone.utc)
            asset = Asset(
                id=asset_id,
                transcode_job_id=job_id,
                master_playlist_url=master_playlist_url,
                thumbnail_url=thumbnail_url,
                renditions=renditions_payload,
                duration_seconds=result.duration_seconds,
            )
            db.add(asset)

        if callback_url:
            _send_webhook(callback_url, {
                "job_id": job_id,
                "caller_reference": caller_reference,
                "status": "ready",
                "asset": {
                    "id": asset_id,
                    "master_playlist_url": master_playlist_url,
                    "thumbnail_url": thumbnail_url,
                    "duration_seconds": result.duration_seconds,
                },
            })

    except (FfmpegError, S3UploadError, FileNotFoundError) as exc:
        _mark_failed(job_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - last-resort catch-all, see _mark_failed's own doc comment
        logger.exception("Unexpected error in transcode_video for job %s", job_id)
        _mark_failed(job_id, f"Unexpected error: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
