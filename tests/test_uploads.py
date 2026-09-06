import base64
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config.settings import settings
from app.db import get_db
from app.main import app
from app.models.asset import TranscodeJob
from app.models.base import Base
from app.models.upload import UploadSession

TEST_TOKEN = "test-internal-token"
AUTH_HEADERS = {"X-Internal-Auth": TEST_TOKEN}


@pytest.fixture(scope="module")
def db_engine():
    # Uses the same DATABASE_URL as the running app (a real local Postgres,
    # not sqlite) - tus's PATCH handler does real file I/O against
    # settings.upload_staging_dir, and the whole point of these tests is
    # exercising the actual resumable-write/offset-tracking behavior, not
    # a mocked approximation of it.
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db_session(db_engine):
    connection = db_engine.connect()
    transaction = connection.begin()
    TestSession = sessionmaker(bind=connection)
    session = TestSession()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "upload_staging_dir", str(tmp_path))

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _metadata_header(**fields: str) -> str:
    return ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in fields.items())


class TestAuth:
    def test_missing_token_rejected(self, client):
        response = client.post("/uploads", headers={"Upload-Length": "10", "X-Owner-User-Id": "u1"})
        assert response.status_code == 401

    def test_wrong_token_rejected(self, client):
        response = client.post(
            "/uploads",
            headers={"Upload-Length": "10", "X-Owner-User-Id": "u1", "X-Internal-Auth": "wrong"},
        )
        assert response.status_code == 401


class TestCreateUpload:
    def test_creates_session_and_returns_location(self, client, db_session):
        response = client.post(
            "/uploads",
            headers={
                **AUTH_HEADERS,
                "Upload-Length": "12",
                "Upload-Metadata": _metadata_header(filename="clip.mp4", filetype="video/mp4"),
                "X-Owner-User-Id": "user-42",
            },
        )
        assert response.status_code == 201
        assert response.headers["Tus-Resumable"] == "1.0.0"
        assert "/uploads/" in response.headers["Location"]

        upload_id = response.headers["Location"].rstrip("/").split("/")[-1]
        session = db_session.get(UploadSession, upload_id)
        assert session is not None
        assert session.filename == "clip.mp4"
        assert session.content_type == "video/mp4"
        assert session.total_bytes == 12
        assert session.offset_bytes == 0
        assert session.owner_user_id == "user-42"
        assert os.path.exists(session.staging_path)

    def test_stores_optional_callback_fields(self, client, db_session):
        response = client.post(
            "/uploads",
            headers={
                **AUTH_HEADERS,
                "Upload-Length": "5",
                "X-Owner-User-Id": "user-1",
                "X-Callback-Url": "https://django.internal/hooks/video-ready",
                "X-Caller-Reference": "content-abc-123",
            },
        )
        upload_id = response.headers["Location"].rstrip("/").split("/")[-1]
        session = db_session.get(UploadSession, upload_id)
        assert session.callback_url == "https://django.internal/hooks/video-ready"
        assert session.caller_reference == "content-abc-123"

    def test_rejects_missing_upload_length(self, client):
        response = client.post("/uploads", headers={**AUTH_HEADERS, "X-Owner-User-Id": "u1"})
        assert response.status_code == 422


class TestHeadUpload:
    def test_returns_current_offset(self, client):
        create = client.post("/uploads", headers={**AUTH_HEADERS, "Upload-Length": "10", "X-Owner-User-Id": "u1"})
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        response = client.head(f"/uploads/{upload_id}", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.headers["Upload-Offset"] == "0"
        assert response.headers["Upload-Length"] == "10"
        assert response.headers["Cache-Control"] == "no-store"

    def test_404_for_unknown_upload(self, client):
        response = client.head("/uploads/does-not-exist", headers=AUTH_HEADERS)
        assert response.status_code == 404


class TestPatchUpload:
    def test_appends_bytes_and_advances_offset(self, client, db_session):
        body = b"hello world!"  # 12 bytes
        create = client.post(
            "/uploads",
            headers={**AUTH_HEADERS, "Upload-Length": str(len(body)), "X-Owner-User-Id": "u1"},
        )
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        # Sending the whole body in one PATCH reaches completion, which
        # enqueues a real Celery task - mocked here since this test is
        # about the byte-write/offset behavior, not the queue integration
        # (that's covered separately in test_completion_creates_transcode_job_and_enqueues_task).
        with patch("app.workers.celery_app.celery_app.send_task"):
            response = client.patch(
                f"/uploads/{upload_id}",
                headers={
                    **AUTH_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=body,
            )
        assert response.status_code == 204
        assert response.headers["Upload-Offset"] == str(len(body))

        session = db_session.get(UploadSession, upload_id)
        with open(session.staging_path, "rb") as fh:
            assert fh.read() == body

    def test_resumes_from_partial_offset(self, client, db_session):
        body = b"0123456789"
        create = client.post(
            "/uploads",
            headers={**AUTH_HEADERS, "Upload-Length": str(len(body)), "X-Owner-User-Id": "u1"},
        )
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        first = client.patch(
            f"/uploads/{upload_id}",
            headers={**AUTH_HEADERS, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=body[:4],
        )
        assert first.headers["Upload-Offset"] == "4"

        head = client.head(f"/uploads/{upload_id}", headers=AUTH_HEADERS)
        assert head.headers["Upload-Offset"] == "4"

        # The second PATCH completes the upload (4 + 6 == 10 == total_bytes),
        # which enqueues a real Celery task - mocked for the same reason as
        # the test above.
        with patch("app.workers.celery_app.celery_app.send_task"):
            second = client.patch(
                f"/uploads/{upload_id}",
                headers={**AUTH_HEADERS, "Upload-Offset": "4", "Content-Type": "application/offset+octet-stream"},
                content=body[4:],
            )
        assert second.status_code == 204
        assert second.headers["Upload-Offset"] == "10"

        session = db_session.get(UploadSession, upload_id)
        with open(session.staging_path, "rb") as fh:
            assert fh.read() == body

    def test_offset_mismatch_rejected_with_409(self, client):
        create = client.post("/uploads", headers={**AUTH_HEADERS, "Upload-Length": "10", "X-Owner-User-Id": "u1"})
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        response = client.patch(
            f"/uploads/{upload_id}",
            headers={**AUTH_HEADERS, "Upload-Offset": "5", "Content-Type": "application/offset+octet-stream"},
            content=b"12345",
        )
        assert response.status_code == 409

    def test_wrong_content_type_rejected(self, client):
        create = client.post("/uploads", headers={**AUTH_HEADERS, "Upload-Length": "5", "X-Owner-User-Id": "u1"})
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        response = client.patch(
            f"/uploads/{upload_id}",
            headers={**AUTH_HEADERS, "Upload-Offset": "0", "Content-Type": "text/plain"},
            content=b"hello",
        )
        assert response.status_code == 415

    def test_rejects_bytes_exceeding_declared_length(self, client):
        create = client.post("/uploads", headers={**AUTH_HEADERS, "Upload-Length": "3", "X-Owner-User-Id": "u1"})
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        response = client.patch(
            f"/uploads/{upload_id}",
            headers={**AUTH_HEADERS, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=b"way too many bytes",
        )
        assert response.status_code == 400

    def test_completion_creates_transcode_job_and_enqueues_task(self, client, db_session):
        body = b"tiny-fake-video-bytes"
        create = client.post(
            "/uploads",
            headers={
                **AUTH_HEADERS,
                "Upload-Length": str(len(body)),
                "X-Owner-User-Id": "u1",
                "X-Callback-Url": "https://django.internal/hooks/ready",
                "X-Caller-Reference": "ref-1",
            },
        )
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        with patch("app.workers.celery_app.celery_app.send_task") as send_task:
            response = client.patch(
                f"/uploads/{upload_id}",
                headers={**AUTH_HEADERS, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
                content=body,
            )
            assert response.status_code == 204

            session = db_session.get(UploadSession, upload_id)
            assert session.status == "transcode_queued"
            assert session.completed_at is not None

            job = db_session.query(TranscodeJob).filter_by(upload_session_id=upload_id).one()
            assert job.callback_url == "https://django.internal/hooks/ready"
            assert job.caller_reference == "ref-1"

            send_task.assert_called_once_with("transcode_video", args=[job.id])

    def test_patch_after_completion_rejected(self, client):
        body = b"x"
        create = client.post("/uploads", headers={**AUTH_HEADERS, "Upload-Length": "1", "X-Owner-User-Id": "u1"})
        upload_id = create.headers["Location"].rstrip("/").split("/")[-1]

        with patch("app.workers.celery_app.celery_app.send_task"):
            client.patch(
                f"/uploads/{upload_id}",
                headers={**AUTH_HEADERS, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
                content=body,
            )

        response = client.patch(
            f"/uploads/{upload_id}",
            headers={**AUTH_HEADERS, "Upload-Offset": "1", "Content-Type": "application/offset+octet-stream"},
            content=b"y",
        )
        assert response.status_code == 410
