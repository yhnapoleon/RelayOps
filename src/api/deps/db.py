"""Database session dependency.

Yields a per-request SQLAlchemy Session and manages the transaction boundary:
- commit on success
- rollback on exception
- close in finally

Routes/services should never call session.commit() / rollback() directly.
"""

from typing import Generator

from sqlalchemy.orm import Session

from core.models.database import get_db


def get_session() -> Generator[Session, None, None]:
    session = get_db().get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
