from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_internal_auth
from app.db import get_db
from app.models.asset import Asset

router = APIRouter(prefix="/assets", tags=["assets"], dependencies=[Depends(require_internal_auth)])


class Rendition(BaseModel):
    height: int
    bitrate_kbps: int
    playlist_url: str


class AssetResponse(BaseModel):
    id: str
    master_playlist_url: str
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    renditions: list[Rendition] = []


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: str, db: Session = Depends(get_db)) -> AssetResponse:
    """Direct asset lookup by id - for a caller that already knows the
    asset id (e.g. persisted from a webhook payload, see jobs.py's
    callback-config) and just wants current playback info, without going
    through the job it came from. GET /jobs/{id} covers the same fields
    inline for the common "just finished transcoding" case; this is the
    equivalent for whenever a caller already has the asset id specifically.
    """
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return AssetResponse(
        id=asset.id,
        master_playlist_url=asset.master_playlist_url,
        thumbnail_url=asset.thumbnail_url,
        duration_seconds=asset.duration_seconds,
        renditions=[Rendition(**r) for r in (asset.renditions or [])],
    )
