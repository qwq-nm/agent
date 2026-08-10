from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database not in {None, ":memory:"}:
        Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
