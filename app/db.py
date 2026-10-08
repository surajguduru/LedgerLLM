from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


def _make_engine(url: str):
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    else:
        # Neon's free tier caps connections and suspends idle computes: keep the pool small and
        # recycle connections before the server drops them (pre_ping catches the rest).
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=300)
    return create_engine(url, **kwargs)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create all tables. The schema is pre-designed in app/models.py; we deliberately skip Alembic
    for this project (see docs/DESIGN.md, decision D4). Dev DBs are throwaway."""
    from app import models

    models.Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
