import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registers every table with Base.metadata, see app/models/__init__.py
from app.config.settings import settings
from app.db import get_db
from app.main import app
from app.models.base import Base

TEST_TOKEN = "test-internal-token"


@pytest.fixture()
def db_session():
    # In-memory SQLite per test - fast, no external Postgres dependency for
    # this workstream's own tests. StaticPool + check_same_thread=False so
    # the single in-memory connection survives across the multiple
    # connections SQLAlchemy's default pooling would otherwise open (each
    # of which would see an *empty* :memory: database of its own).
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c.headers.update({"X-Internal-Auth": TEST_TOKEN})
        yield c
    app.dependency_overrides.clear()
