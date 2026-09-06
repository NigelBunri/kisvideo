"""S3 client wrapper for uploading finished renditions/playlists/thumbnails.

Mirrors backend/kis's apps/media/storage_backends.py::S3MediaStorage for the
pieces that matter here — regional endpoint construction (a presigned/plain
request against the wrong host is the documented cause of "upload succeeds,
playback 404s" for any non-us-east-1 bucket), and the same public-URL
format precedent. Not a port of the whole class: that one is a Django
Storage backend (open/save/delete/exists — file-object-oriented, for
Django's ORM FileField machinery); this service only ever needs "upload
this local file, get back the URL a player can fetch directly," so it's a
much smaller surface.

HLS playback fundamentally requires every playlist/segment URL to be
fetchable by a plain, unauthenticated GET — no HLS player does per-segment
presigned-URL refreshing out of the box, unlike a single downloadable
attachment. This is therefore always uploaded public-read, independent of
whatever AWS_S3_PUBLIC_BUCKET is set to for Django's own (different) media
bucket — that setting is a per-bucket, per-use-case choice; nothing here
assumes or depends on Django's bucket being public too.
"""

from __future__ import annotations

import mimetypes
import os
from urllib.parse import quote

import boto3
from botocore.config import Config

from app.config.settings import settings


class S3UploadError(Exception):
    """Raised for any upload failure — callers (the Celery tasks) catch
    this to set TranscodeJob.status = 'failed' with a real message rather
    than letting a raw boto3/botocore exception (often a wall of retry
    metadata) become the stored error_message."""


def _client():
    if not settings.aws_storage_bucket_name:
        raise S3UploadError("AWS_STORAGE_BUCKET_NAME is not configured.")
    endpoint_url = settings.aws_s3_endpoint_url or f"https://s3.{settings.aws_s3_region_name}.amazonaws.com"
    kwargs = {}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client(
        "s3",
        region_name=settings.aws_s3_region_name,
        endpoint_url=endpoint_url,
        config=Config(signature_version="s3v4"),
        **kwargs,
    )


def public_url(key: str) -> str:
    """Same precedence as S3MediaStorage.url() for a public bucket: a CDN
    custom domain if configured, else the bucket's own regional S3 URL."""
    safe_key = quote(key, safe="/")
    if settings.aws_s3_custom_domain:
        return f"https://{settings.aws_s3_custom_domain}/{safe_key}"
    if settings.aws_s3_endpoint_url:
        endpoint = settings.aws_s3_endpoint_url.rstrip("/")
        return f"{endpoint}/{quote(settings.aws_storage_bucket_name, safe='')}/{safe_key}"
    return f"https://{settings.aws_storage_bucket_name}.s3.{settings.aws_s3_region_name}.amazonaws.com/{safe_key}"


def upload_file(local_path: str, key: str) -> str:
    """Uploads one local file to `key`, public-read, with a best-guess
    Content-Type (HLS players are picky about .m3u8/.m4s/.mp4 being served
    with a sane type, unlike a generic downloadable attachment where a
    fallback octet-stream is harmless) — returns the object's public URL.
    """
    content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    try:
        _client().upload_file(
            local_path,
            settings.aws_storage_bucket_name,
            key,
            ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
        )
    except Exception as exc:
        raise S3UploadError(f"Failed to upload {os.path.basename(local_path)} to s3://{key}: {exc}") from exc
    return public_url(key)


def upload_directory(local_dir: str, key_prefix: str) -> dict[str, str]:
    """Uploads every file directly inside local_dir (non-recursive — each
    rendition's own segment directory is flat, no nested subfolders) under
    key_prefix, preserving filenames. Returns {filename: public_url} so
    callers can look up e.g. "720p.m3u8"'s uploaded URL without
    reconstructing the key themselves."""
    urls: dict[str, str] = {}
    for filename in sorted(os.listdir(local_dir)):
        local_path = os.path.join(local_dir, filename)
        if not os.path.isfile(local_path):
            continue
        key = f"{key_prefix.rstrip('/')}/{filename}"
        urls[filename] = upload_file(local_path, key)
    return urls
