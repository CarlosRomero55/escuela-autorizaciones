from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(url: str) -> sessionmaker[Session]:
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """Sesión por petición. Las rutas hacen commit explícito; si no lo hacen, se descarta."""
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
