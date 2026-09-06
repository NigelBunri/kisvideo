"""Resumable upload API — implements the tus resumable upload protocol
(tus.io) Core Protocol plus the Creation extension. See ARCHITECTURE.md
for why tus rather than a custom chunking scheme.

Deliberately NOT implemented (out of v1 scope, no client here needs them):
- Deferred-length uploads (Upload-Defer-Length) - every caller (Django/
  Nest) already knows the file size up front from the client's own upload
  picker, so there's no case where length is genuinely unknown at creation.
- Upload-Concat (parallel/concatenated uploads) - a single sequential
  stream per upload is all this needs.
- Termination extension (DELETE) - an abandoned upload is cleaned up by
  TTL expiry (see the staging-cleanup note on UploadSession.status), not
  a client-initiated delete.
"""

import base64
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import require_internal_auth
from app.config.settings import settings
from app.db import get_db
from app.models.asset import TranscodeJob
from app.models.upload import UploadSession

router = APIRouter(prefix="/uploads", tags=["uploads"], dependencies=[Depends(require_internal_auth)])

TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation"


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Tus-Resumable": TUS_VERSION}
    if extra:
        headers.update(extra)
    return headers


def _parse_upload_metadata(raw: str) -> dict[str, str]:
    """Upload-Metadata: comma-separated `key base64(value)` pairs (tus
    Creation extension format) - e.g. `filename dGVzdC5tcDQ=,filetype dmlkZW8vbXA0`."""
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(" ", 1)
        key = parts[0].strip()
        if not key:
            continue
        if len(parts) == 1:
            result[key] = ""
            continue
        try:
            result[key] = base64.b64decode(parts[1].strip()).decode("utf-8")
        except Exception:
            # Malformed metadata value - the upload can still proceed with
            # whatever we did parse; a corrupt filename isn't a reason to
            # reject an otherwise-valid resumable upload request.
            continue
    return result


def _staging_path(upload_id: str) -> str:
    os.makedirs(settings.upload_staging_dir, exist_ok=True)
    return os.path.join(settings.upload_staging_dir, upload_id)


def _enqueue_transcode(job_id: str) -> None:
    # Local import: avoids a hard dependency on the Celery app (and
    # whatever the transcode task module ends up importing - ffmpeg
    # wrappers, S3 client, etc.) at API-module import time, so `uvicorn
    # app.main:app` can start even before the transcode task module is
    # fully built out (it's developed in parallel - see ARCHITECTURE.md's
    # coordination note). Task name/signature confirmed directly with the
    # transcode pipeline owner: "transcode_video", one positional arg (the
    # TranscodeJob id, a str) - matches their transcode_video.delay(job_id).
    from app.workers.celery_app import celery_app

    celery_app.send_task("transcode_video", args=[job_id])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_upload(
    request: Request,
    response: Response,
    upload_length: int = Header(..., alias="Upload-Length", gt=0),
    upload_metadata: str = Header(default="", alias="Upload-Metadata"),
    owner_user_id: str = Header(..., alias="X-Owner-User-Id"),
    callback_url: str | None = Header(default=None, alias="X-Callback-Url"),
    caller_reference: str | None = Header(default=None, alias="X-Caller-Reference"),
    db: Session = Depends(get_db),
):
    """tus Creation extension. Caller (Django/Nest) declares the total
    size up front; the actual uploader's identity travels as
    X-Owner-User-Id since the internal-auth token identifies the calling
    *service*, not the end user on whose behalf it's uploading.
    X-Callback-Url/X-Caller-Reference are optional - an upload created
    without them still transcodes normally, just falls back to
    GET /jobs/{id} polling instead of a webhook push on completion."""
    metadata = _parse_upload_metadata(upload_metadata)

    session = UploadSession(
        owner_user_id=owner_user_id,
        filename=metadata.get("filename", "upload"),
        content_type=metadata.get("filetype", "application/octet-stream"),
        total_bytes=upload_length,
        offset_bytes=0,
        staging_path="",  # id doesn't exist until after flush() below - filled in immediately after
        callback_url=callback_url,
        caller_reference=caller_reference,
    )
    db.add(session)
    db.flush()  # populate session.id (server-generated UUID default) before we need it for the path

    session.staging_path = _staging_path(session.id)
    # Pre-allocate the staging file up front (not lazily on first PATCH) so
    # a HEAD request against a freshly-created, never-yet-PATCHed upload
    # can't hit a "file doesn't exist" error - an empty file at
    # offset_bytes=0 is exactly the correct starting state.
    open(session.staging_path, "wb").close()
    db.commit()

    location = str(request.url_for("head_upload", upload_id=session.id))
    response.headers["Location"] = location
    for key, value in _tus_headers().items():
        response.headers[key] = value
    return Response(status_code=status.HTTP_201_CREATED, headers=response.headers)


@router.head("/{upload_id}", name="head_upload")
def head_upload(upload_id: str, db: Session = Depends(get_db)):
    session = db.get(UploadSession, upload_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")
    headers = _tus_headers(
        {
            "Upload-Offset": str(session.offset_bytes),
            "Upload-Length": str(session.total_bytes),
            # tus spec: HEAD responses must not be cached, since the offset
            # changes on every successful PATCH.
            "Cache-Control": "no-store",
        }
    )
    return Response(status_code=status.HTTP_200_OK, headers=headers)


@router.patch("/{upload_id}")
async def patch_upload(
    upload_id: str,
    request: Request,
    upload_offset: int = Header(..., alias="Upload-Offset", ge=0),
    content_type: str = Header(default="", alias="Content-Type"),
    db: Session = Depends(get_db),
):
    if content_type != "application/offset+octet-stream":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Type must be application/offset+octet-stream.",
        )

    session = db.get(UploadSession, upload_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")
    if session.status != "uploading":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=f"Upload is '{session.status}', not accepting further chunks.")

    # Core tus semantics: the client's declared offset must match the
    # server's - if a previous PATCH's response was lost in transit, the
    # client will retry with the offset it last confirmed, which by
    # definition equals what the server already has (safe to reject a
    # mismatch as a real conflict, not "just resume from ours" - that
    # would silently accept a client that's confused about its own state).
    if upload_offset != session.offset_bytes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload-Offset {upload_offset} does not match current offset {session.offset_bytes}.",
        )

    bytes_written = 0
    with open(session.staging_path, "r+b") as fh:
        fh.seek(upload_offset)
        async for chunk in request.stream():
            if not chunk:
                continue
            # Never write past the declared total - a client sending more
            # than it originally promised is a protocol violation, not
            # something to silently truncate-and-accept.
            if session.offset_bytes + bytes_written + len(chunk) > session.total_bytes:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded bytes exceed the declared Upload-Length.",
                )
            fh.write(chunk)
            bytes_written += len(chunk)

    session.offset_bytes += bytes_written

    job_id: str | None = None
    if session.offset_bytes >= session.total_bytes:
        from datetime import datetime, timezone

        session.status = "completed"
        session.completed_at = datetime.now(timezone.utc)
        job = TranscodeJob(
            upload_session_id=session.id,
            callback_url=session.callback_url,
            caller_reference=session.caller_reference,
        )
        db.add(job)
        db.flush()
        job_id = job.id
        session.status = "transcode_queued"

    db.commit()

    if job_id:
        _enqueue_transcode(job_id)

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_tus_headers({"Upload-Offset": str(session.offset_bytes)}),
    )
