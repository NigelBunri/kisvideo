from app.models.asset import Asset, TranscodeJob


def _make_ready_asset(db_session) -> Asset:
    job = TranscodeJob(upload_session_id="upload-1", status="ready")
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    asset = Asset(
        transcode_job_id=job.id,
        master_playlist_url="https://cdn.example.com/master.m3u8",
        thumbnail_url="https://cdn.example.com/thumb.jpg",
        duration_seconds=42.0,
        renditions=[
            {"height": 1080, "bitrate_kbps": 5000, "playlist_url": "https://cdn.example.com/1080p.m3u8"},
            {"height": 720, "bitrate_kbps": 2500, "playlist_url": "https://cdn.example.com/720p.m3u8"},
        ],
    )
    db_session.add(asset)
    db_session.commit()
    db_session.refresh(asset)
    return asset


def test_get_asset_requires_auth(client):
    client.headers.pop("X-Internal-Auth")
    response = client.get("/assets/does-not-exist")
    assert response.status_code == 401


def test_get_asset_404_when_missing(client):
    response = client.get("/assets/does-not-exist")
    assert response.status_code == 404


def test_get_asset_returns_playback_info(client, db_session):
    asset = _make_ready_asset(db_session)
    response = client.get(f"/assets/{asset.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == asset.id
    assert body["master_playlist_url"] == "https://cdn.example.com/master.m3u8"
    assert body["thumbnail_url"] == "https://cdn.example.com/thumb.jpg"
    assert body["duration_seconds"] == 42.0
    assert body["renditions"] == [
        {"height": 1080, "bitrate_kbps": 5000, "playlist_url": "https://cdn.example.com/1080p.m3u8"},
        {"height": 720, "bitrate_kbps": 2500, "playlist_url": "https://cdn.example.com/720p.m3u8"},
    ]
