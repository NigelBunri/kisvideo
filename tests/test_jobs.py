from app.models.asset import Asset, TranscodeJob


def _make_job(db_session, **overrides) -> TranscodeJob:
    job = TranscodeJob(
        upload_session_id="upload-1",
        status=overrides.pop("status", "queued"),
        **overrides,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_get_job_requires_auth(client):
    client.headers.pop("X-Internal-Auth")
    job_response = client.get("/jobs/does-not-exist")
    assert job_response.status_code == 401


def test_get_job_rejects_wrong_token(client):
    client.headers.update({"X-Internal-Auth": "wrong-token"})
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 401


def test_get_job_404_when_missing(client):
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 404


def test_get_job_queued_has_no_asset(client, db_session):
    job = _make_job(db_session, status="queued")
    response = client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["asset"] is None


def test_get_job_failed_includes_error_message(client, db_session):
    job = _make_job(db_session, status="failed", error_message="ffmpeg exited 1")
    response = client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "ffmpeg exited 1"
    assert body["asset"] is None


def test_get_job_ready_includes_asset_inline(client, db_session):
    job = _make_job(db_session, status="ready")
    asset = Asset(
        transcode_job_id=job.id,
        master_playlist_url="https://cdn.example.com/master.m3u8",
        thumbnail_url="https://cdn.example.com/thumb.jpg",
        duration_seconds=123.4,
        renditions=[{"height": 1080, "bitrate_kbps": 5000, "playlist_url": "https://cdn.example.com/1080p.m3u8"}],
    )
    db_session.add(asset)
    db_session.commit()

    response = client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["asset"] == {
        "id": asset.id,
        "master_playlist_url": "https://cdn.example.com/master.m3u8",
        "thumbnail_url": "https://cdn.example.com/thumb.jpg",
        "duration_seconds": 123.4,
    }


def test_set_callback_config(client, db_session):
    job = _make_job(db_session, status="queued")
    response = client.post(
        f"/jobs/{job.id}/callback-config",
        json={"callback_url": "https://django.internal/webhooks/kisvideo", "caller_reference": "content-42"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": job.id,
        "callback_url": "https://django.internal/webhooks/kisvideo",
        "caller_reference": "content-42",
    }

    db_session.refresh(job)
    assert job.callback_url == "https://django.internal/webhooks/kisvideo"
    assert job.caller_reference == "content-42"


def test_set_callback_config_404_when_missing(client):
    response = client.post(
        "/jobs/does-not-exist/callback-config",
        json={"callback_url": "https://django.internal/webhooks/kisvideo"},
    )
    assert response.status_code == 404
