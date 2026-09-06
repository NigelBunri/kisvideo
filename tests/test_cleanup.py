import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.config.settings import settings
from app.db import SessionLocal
from app.models.base import Base
from app.models.upload import UploadSession
from app.workers.cleanup import cleanup_abandoned_uploads

# Real local Postgres, not sqlite - same reasoning as test_uploads.py:
# this task does real file-system + DB work and is meant to run for real
# via Celery Beat, not as a mocked approximation.
#
# Deliberately NOT the rollback-in-a-connection-transaction pattern
# test_uploads.py uses: cleanup_abandoned_uploads runs inside its own
# session_scope(), which opens an independent connection from the pool -
# it would never see data that only exists inside this test's own
# uncommitted transaction on a *different* connection. Test rows are
# committed for real and deleted explicitly in teardown instead.


@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db_session(db_engine):
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(UploadSession).delete()
    session.commit()
    session.close()


@pytest.fixture()
def staging_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_staging_dir", str(tmp_path))
    return tmp_path


def _make_session(db_session, staging_dir, *, age_hours: float, status: str = "uploading", filename: str = "x.mp4") -> UploadSession:
    session = UploadSession(
        owner_user_id="u1",
        filename=filename,
        content_type="video/mp4",
        total_bytes=100,
        offset_bytes=10,
        status=status,
        staging_path="",  # NOT NULL - id doesn't exist until after flush, filled in below
    )
    db_session.add(session)
    db_session.flush()
    session.staging_path = str(staging_dir / session.id)
    with open(session.staging_path, "wb") as fh:
        fh.write(b"partial-upload-bytes")
    # created_at has a server_default, so it's only set once actually
    # inserted - overwrite directly to simulate an old row rather than
    # sleeping in a test.
    db_session.execute(
        UploadSession.__table__.update()
        .where(UploadSession.id == session.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours))
    )
    db_session.commit()
    db_session.refresh(session)
    return session


class TestCleanupAbandonedUploads:
    def test_sweeps_expired_uploading_session_and_deletes_file(self, db_session, staging_dir):
        old = _make_session(db_session, staging_dir, age_hours=settings.upload_ttl_hours + 1)

        result = cleanup_abandoned_uploads.run()

        assert result["swept"] == 1
        db_session.refresh(old)
        assert old.status == "expired"
        assert not os.path.exists(old.staging_path)

    def test_does_not_sweep_recent_uploading_session(self, db_session, staging_dir):
        recent = _make_session(db_session, staging_dir, age_hours=1)

        result = cleanup_abandoned_uploads.run()

        assert result["swept"] == 0
        db_session.refresh(recent)
        assert recent.status == "uploading"
        assert os.path.exists(recent.staging_path)

    def test_does_not_sweep_completed_sessions_even_if_old(self, db_session, staging_dir):
        completed = _make_session(db_session, staging_dir, age_hours=999, status="transcode_queued")

        result = cleanup_abandoned_uploads.run()

        assert result["swept"] == 0
        db_session.refresh(completed)
        assert completed.status == "transcode_queued"
        assert os.path.exists(completed.staging_path)

    def test_handles_row_whose_file_is_already_gone(self, db_session, staging_dir):
        old = _make_session(db_session, staging_dir, age_hours=settings.upload_ttl_hours + 1)
        os.remove(old.staging_path)

        result = cleanup_abandoned_uploads.run()

        assert result["swept"] == 1
        assert result["file_already_missing"] == 1
        db_session.refresh(old)
        assert old.status == "expired"

    def test_sweeps_multiple_expired_sessions_in_one_run(self, db_session, staging_dir):
        a = _make_session(db_session, staging_dir, age_hours=48, filename="a.mp4")
        b = _make_session(db_session, staging_dir, age_hours=72, filename="b.mp4")
        _make_session(db_session, staging_dir, age_hours=1, filename="c.mp4")  # not expired

        result = cleanup_abandoned_uploads.run()

        assert result["swept"] == 2
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.status == "expired"
        assert b.status == "expired"
