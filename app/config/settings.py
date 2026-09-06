from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shared-secret auth for Django/Nest -> this service calls, same trust
    # model as DJANGO_INTERNAL_TOKEN between Django and Nest today.
    internal_token: str = ""

    database_url: str = "postgresql+psycopg://kis_video:kis_video@localhost:5432/kis_video"
    redis_url: str = "redis://localhost:6379/0"

    s3_bucket: str = ""
    s3_region: str = "eu-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # Where completed uploads are staged before a transcode job picks them
    # up. A local path in dev; should be a durable volume in production
    # (or the upload could land directly in S3 - a decision for whoever
    # builds the tus endpoint to make explicitly, not silently assume).
    upload_staging_dir: str = "/tmp/kis-video-uploads"


settings = Settings()
