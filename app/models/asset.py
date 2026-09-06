import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TranscodeJob(Base):
    """One transcode attempt for one completed upload. A single
    UploadSession maps to exactly one TranscodeJob under normal operation
    - kept as a separate table (not fields bolted onto UploadSession)
    because a failed job may need to be retried as a fresh row without
    losing the original upload's record."""

    __tablename__ = "transcode_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    upload_session_id: Mapped[str] = mapped_column(String(36), ForeignKey("upload_sessions.id"), index=True)
    # 'queued' -> 'probing' -> 'transcoding' -> 'ready' | 'failed'. Poll via
    # GET /jobs/{id}; also pushed via webhook on 'ready'/'failed' - see
    # ARCHITECTURE.md's notify step.
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    # Populated by the probe step (ffprobe) before any transcoding starts.
    source_duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Internal callback target once this job resolves - who to notify (e.g.
    # Django's internal webhook URL + a caller-supplied reference id so the
    # caller can match the callback back to its own record without needing
    # our job id to be the primary key on their side too).
    callback_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    caller_reference: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Asset(Base):
    """The finished, playable output of one successful TranscodeJob. One
    row per job - `renditions` and `master_playlist_url` are the two
    fields kistube-website's HlsVideo.tsx actually needs (a single HLS
    master-playlist URL is all a player requires; `renditions` is kept
    for admin/debug visibility, not for playback)."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    transcode_job_id: Mapped[str] = mapped_column(String(36), ForeignKey("transcode_jobs.id"), unique=True)
    master_playlist_url: Mapped[str] = mapped_column(String(1024))
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # [{"height": 1080, "bitrate_kbps": 5000, "playlist_url": "..."}, ...]
    # - one entry per rendition actually produced (only renditions <=
    # source resolution, per ARCHITECTURE.md).
    renditions: Mapped[list] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
