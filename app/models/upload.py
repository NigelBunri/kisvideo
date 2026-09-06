import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadSession(Base):
    """One tus resumable-upload session. `id` doubles as the tus resource
    id in the upload URL (PATCH /uploads/{id}). `offset_bytes` is the
    single source of truth for "how much have we received" - the tus
    HEAD response reads directly from this column, never from re-checking
    the staged file's size on disk (which could be mid-write)."""

    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    total_bytes: Mapped[int] = mapped_column(Integer)
    offset_bytes: Mapped[int] = mapped_column(Integer, default=0)
    staging_path: Mapped[str] = mapped_column(String(1024))
    # 'uploading' -> 'completed' -> ('transcode_queued' once a TranscodeJob
    # is created for it). A session that never reaches 'completed' within
    # its TTL is eligible for staging-file cleanup - see whoever builds the
    # upload API for the actual expiry sweep.
    status: Mapped[str] = mapped_column(String(32), default="uploading")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
