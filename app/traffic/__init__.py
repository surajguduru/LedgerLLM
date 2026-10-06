from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session


def dialect_insert(db: Session):
    """The INSERT constructor with ON CONFLICT support for the bound database (Postgres or SQLite)."""
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
