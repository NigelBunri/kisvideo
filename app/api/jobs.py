from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_internal_auth
from app.db import get_db
from app.models.asset import Asset, TranscodeJob

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_internal_auth)])


class AssetSummary(BaseModel):
    """Just the fields a caller actually needs to start playback - see
    Asset's own model comment for why `renditions` is left out here (admin/
    debug visibility only, not needed for playback)."""

    id: str
    master_playlist_url: str
    thumbnail_url: str | None = None
    duration_seconds: float | None = None


class JobStatusResponse(BaseModel):
    id: str
    status: str
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    # Populated only once status == "ready" - the whole point of inlining
    # this is so a caller polling GET /jobs/{id} doesn't need a second
    # request to GET /assets/{id} the moment they see "ready".
    asset: AssetSummary | None = None


class CallbackConfigRequest(BaseModel):
    callback_url: str
    caller_reference: str | None = None


class CallbackConfigResponse(BaseModel):
    id: str
    callback_url: str
    caller_reference: str | None = None


def _load_job_or_404(db: Session, job_id: str) -> TranscodeJob:
    job = db.get(TranscodeJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobStatusResponse:
    job = _load_job_or_404(db, job_id)

    asset_summary: AssetSummary | None = None
    if job.status == "ready":
        # transcode_job_id is a unique FK (one Asset per job) - a "ready" job
        # with no matching Asset row would be a real inconsistency (a worker
        # bug flipping status before the Asset write commits), not a normal
        # caller-facing case, so this isn't wrapped in a soft fallback: it
        # surfaces as a 500 the same way any other invariant violation would.
        asset = db.query(Asset).filter(Asset.transcode_job_id == job.id).one()
        asset_summary = AssetSummary(
            id=asset.id,
            master_playlist_url=asset.master_playlist_url,
            thumbnail_url=asset.thumbnail_url,
            duration_seconds=asset.duration_seconds,
        )

    return JobStatusResponse(
        id=job.id,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
        asset=asset_summary,
    )


@router.post("/{job_id}/callback-config", response_model=CallbackConfigResponse)
def set_callback_config(
    job_id: str, payload: CallbackConfigRequest, db: Session = Depends(get_db)
) -> CallbackConfigResponse:
    """Registers where this job's transcode pipeline should webhook on
    'ready'/'failed' (see TranscodeJob.callback_url/caller_reference and
    ARCHITECTURE.md's notify step). A dedicated endpoint rather than folding
    this into upload/job creation - keeps it callable at any point after the
    job exists (including for a caller who didn't have a callback URL handy
    yet at upload time, or wants to change it), and keeps this workstream's
    API self-contained instead of requiring a shape change to the upload
    endpoint another workstream owns.
    """
    job = _load_job_or_404(db, job_id)
    job.callback_url = payload.callback_url
    job.caller_reference = payload.caller_reference
    db.commit()
    return CallbackConfigResponse(
        id=job.id,
        callback_url=job.callback_url,
        caller_reference=job.caller_reference,
    )
