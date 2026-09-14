"""Database setup."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.core.config import get_settings

settings = get_settings()

# ``timeout`` is SQLite's busy timeout: a writer waits for the lock instead of
# failing immediately ("database is locked") while a long stream run is writing.
# ``isolation_level="IMMEDIATE"`` makes every write transaction take the write
# lock up front (BEGIN IMMEDIATE). Without it a request that first SELECTs and
# then UPDATEs can hit SQLITE_BUSY_SNAPSHOT as soon as a stream run commits in
# between — the write then fails instantly, ignoring the busy timeout.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"timeout": 30, "isolation_level": "IMMEDIATE"},
)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    """SQLite tuning so a multi-minute stream run cannot block the whole app.

    * WAL lets readers (the UI) work while a run writes;
    * ``busy_timeout`` makes competing writers wait instead of erroring out;
    * NORMAL synchronous is the recommended pairing with WAL.
    """
    if engine.url.get_backend_name() != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()