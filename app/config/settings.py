from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shared-secret auth for Django/Nest -> this service calls, same trust
    # model as DJANGO_INTERNAL_TOKEN between Django and Nest today.
    internal_token: str = ""

    database_url: str = "postgresql+psycopg://kis_video:kis_video@localhost:5432/kis_video"
    redis_url: str = "redis://localhost:6379/0"

    # Field names match backend/kis's actual S3 env vars exactly (see
    # apps/media/storage_backends.py) — pydantic-settings maps a field to
    # its uppercased name by default, so aws_storage_bucket_name reads
    # AWS_STORAGE_BUCKET_NAME with no alias needed. The original s3_bucket/
    # s3_region names here would have read S3_BUCKET/S3_REGION instead —
    # different env vars than what's actually deployed for Django/Nest
    # today, which is exactly the "invented a new convention instead of
    # reusing the existing one" ARCHITECTURE.md/dev-3c's task both warned
    # against.
    aws_storage_bucket_name: str = ""
    aws_s3_region_name: str = "eu-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Optional — only set for an S3-compatible non-AWS endpoint, or to
    # front the bucket with a CDN domain. Both default to AWS's own S3
    # endpoint / the bucket's own S3 domain when unset, same fallback
    # behavior as S3MediaStorage on the Django side.
    aws_s3_endpoint_url: str = ""
    aws_s3_custom_domain: str = ""

    # Where completed uploads are staged before a transcode job picks them
    # up. A local path in dev; should be a durable volume in production
    # (or the upload could land directly in S3 - a decision for whoever
    # builds the tus endpoint to make explicitly, not silently assume).
    upload_staging_dir: str = "/tmp/kis-video-uploads"

    # A tus session still in 'uploading' status older than this is
    # considered abandoned (dropped connection, client crash, a user who
    # just never came back) - see app/workers/cleanup.py's periodic sweep.
    # 24h is generous enough that a genuinely slow/interrupted-but-still-
    # resuming upload on a bad connection won't get swept out from under
    # a client that's still trying, while not leaving abandoned files on
    # disk indefinitely.
    upload_ttl_hours: int = 24


settings = Settings()
