from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import settings

# pool_pre_ping: this service talks to its own dedicated Postgres, but the
# same "connection went stale after idle" failure mode that motivates
# CONN_MAX_AGE handling on the Django side applies here too - cheap
# insurance against a long-lived worker/api process holding a dead
# connection open.
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Celery-task-facing equivalent of get_db above. Tasks run outside
    FastAPI's request lifecycle, so there's no dependency-injection
    mechanism to yield a session through — this is a plain context manager
    instead: `with session_scope() as db: ...`. Commits on clean exit,
    rolls back on any exception, always closes — every worker task in
    app/workers/ should wrap its DB work in this rather than opening a bare
    SessionLocal() directly, so a mid-task exception can never leave a
    half-written transaction open against the connection pool."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
