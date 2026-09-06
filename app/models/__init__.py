# Every model module must be imported here (even though nothing in this
# file references the names directly) - SQLAlchemy's declarative Base only
# registers a table with Base.metadata once its model class has actually
# been imported somewhere. Without this, Base.metadata.create_all() (used
# by every workstream's own test suite, and eventually Alembic's
# autogenerate) only sees whichever models happened to be imported by
# whatever else that specific test/script imported first - e.g. a test
# that only imports app.models.asset would fail to resolve TranscodeJob's
# FK to upload_sessions, since UploadSession's table was never registered.
from app.models.asset import Asset, TranscodeJob  # noqa: F401
from app.models.upload import UploadSession  # noqa: F401
