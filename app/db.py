from collections.abc import Generator

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
