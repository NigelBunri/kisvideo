"""Regression test for a 2026-09-07 real production finding: upload_file
used to pass ACL="public-read" on every PutObject, which the real target
bucket (S3 Object Ownership: "Bucket owner enforced") rejects outright
with AccessControlListNotSupported — every real transcode job failed at
the S3-upload step. Public readability must come from a bucket policy on
this service's key prefix instead, not a per-object ACL.
"""
from unittest.mock import MagicMock, patch

from app.storage import s3_client


def test_upload_file_does_not_set_acl(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_client.settings, "aws_storage_bucket_name", "test-bucket")
    monkeypatch.setattr(s3_client.settings, "aws_s3_region_name", "eu-west-2")
    monkeypatch.setattr(s3_client.settings, "aws_s3_custom_domain", "")
    monkeypatch.setattr(s3_client.settings, "aws_s3_endpoint_url", "")

    local_file = tmp_path / "master.m3u8"
    local_file.write_text("#EXTM3U")

    mock_client = MagicMock()
    with patch.object(s3_client, "_client", return_value=mock_client):
        s3_client.upload_file(str(local_file), "videos/abc/master.m3u8")

    _, kwargs = mock_client.upload_file.call_args
    extra_args = kwargs["ExtraArgs"]
    assert "ACL" not in extra_args, "upload_file must not set ACL - the real bucket rejects any ACL at all"
    assert extra_args["ContentType"]
