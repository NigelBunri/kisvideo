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
attachment. Public readability is expected to come from a bucket policy
on this service's key prefix, NOT a per-object ACL — the real production
bucket (2026-09-07) has S3 Object Ownership set to "Bucket owner
enforced", which rejects any PutObject carrying an ACL at all, so
upload_file() below deliberately does not set one. See its own docstring
for the full story and what to verify (a real bucket-policy check) before
trusting a "ready" asset's playback URL.
"""

from __future__ import annotations

import mimetypes
import os
from urllib.parse import quote

import boto3
from botocore.config import Config

from app.config.settings import settings

# Python's stdlib mimetypes has no entry for .m4s (fMP4/CMAF segments) at
# all — guess_type() returns (None, None) for it, silently falling through
# to the generic application/octet-stream fallback below. Confirmed via
# direct testing, not assumed. video/iso.segment is the type actually used
# for these across HLS tooling (Akamai's own HLS packaging docs, hls.js);
# most players tolerate a wrong/missing type for segments in practice, but
# there's no reason to rely on that tolerance when the real type is known
# and cheap to set explicitly. Checked before falling through to
# mimetypes.guess_type() for anything not in this dict.
_EXTENSION_CONTENT_TYPE_OVERRIDES = {
    ".m4s": "video/iso.segment",
}


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
    """Uploads one local file to `key`, with a best-guess Content-Type
    (HLS players are picky about .m3u8/.m4s/.mp4 being served with a sane
    type, unlike a generic downloadable attachment where a fallback
    octet-stream is harmless) — returns the object's public URL.

    Does NOT set ACL="public-read" — found via a real production deploy
    (2026-09-07) that the actual target bucket has S3 Object Ownership set
    to "Bucket owner enforced", which rejects any PutObject carrying an
    ACL at all (AccessControlListNotSupported), regardless of the ACL's
    value. Same bucket Django's own S3MediaStorage writes to, and that
    code only sets ACL when self.public_bucket is true — false in this
    exact production config, which is exactly why Django's own uploads
    never hit this. Public readability for HLS playback (this service's
    whole reason for returning an unsigned public_url() rather than a
    signed one) must come from a bucket policy on this key prefix instead
    of a per-object ACL - verify that policy actually grants public GET on
    this prefix before trusting a "ready" asset's playback URL; an
    upload succeeding here no longer proves the URL is actually fetchable.
    """
    ext = os.path.splitext(key)[1].lower()
    content_type = (
        _EXTENSION_CONTENT_TYPE_OVERRIDES.get(ext)
        or mimetypes.guess_type(key)[0]
        or "application/octet-stream"
    )
    try:
        _client().upload_file(
            local_path,
            settings.aws_storage_bucket_name,
            key,
            ExtraArgs={"ContentType": content_type},
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
