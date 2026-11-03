"""Unit tests for the connection-pool sizing of app.core.database.

The unit replaces CICS file control over the CARDDEMO files of csd/CARDDEMO.CSD, as specified in
the Technical Specification, Sections 0.6.10 and 0.6.12: a request holds up to four pooled
connections (its session, the batch-window connection and two write units), and GetSession
admits at most ADMITTED_REQUEST_LIMIT requests at once. These tests pin that the pool keeps
every connection the admitted requests can hold at once, so steady load reuses warm connections
instead of opening and closing overflow ones, that only the closing-session headroom overflows,
that admission still leaves that headroom within the pool's capacity, and that BuildEngine
applies the sizing without connecting. No test touches a database. Every key and password is
generated at run time with the secrets module, and backend/.env is never read (_env_file=None).
"""

import operator
import secrets

import pytest
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core import database
from app.core.config import Settings

# Nine admitted requests, the admission the pool resizing leaves unchanged.
EXPECTED_ADMITTED_REQUEST_LIMIT = 9
ADMITTED_PEAK_CONNECTIONS = database.ADMITTED_REQUEST_LIMIT * database.REQUEST_CONNECTION_LIMIT


def BuildDatabaseUrl(driverName, databasePassword):
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://pool_test:{databasePassword}@127.0.0.1/pool_test"


@pytest.fixture(name="poolSettings")
def PoolSettings():
    """Return backend Settings holding a generated SECRET_KEY and database password."""
    databasePassword = secrets.token_urlsafe(12)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(32),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


@pytest.mark.parametrize(
    ("comparison", "leftValue", "rightValue"),
    [
        (operator.le, ADMITTED_PEAK_CONNECTIONS, database.POOL_SIZE),
        (
            operator.eq,
            ADMITTED_PEAK_CONNECTIONS + database.POOL_HEADROOM_CONNECTIONS,
            database.POOL_CAPACITY,
        ),
        (operator.eq, database.POOL_SIZE + database.POOL_MAX_OVERFLOW, database.POOL_CAPACITY),
        (operator.eq, database.POOL_MAX_OVERFLOW, database.POOL_HEADROOM_CONNECTIONS),
        (operator.eq, database.ADMITTED_REQUEST_LIMIT, EXPECTED_ADMITTED_REQUEST_LIMIT),
    ],
    ids=[
        "admitted-peak-stays-pooled",
        "admitted-peak-plus-headroom-is-capacity",
        "pool-plus-overflow-is-capacity",
        "only-headroom-overflows",
        "admission-limit-unchanged",
    ],
)
def test_pool_sizing_keeps_admitted_peak_pooled(comparison, leftValue, rightValue):
    assert comparison(leftValue, rightValue), (leftValue, rightValue)


async def test_build_engine_applies_pool_sizing_without_connecting(poolSettings):
    engine = database.BuildEngine(poolSettings)
    try:
        enginePool = engine.sync_engine.pool
        assert isinstance(enginePool, AsyncAdaptedQueuePool)
        assert enginePool.size() == database.POOL_SIZE
        # QueuePool exposes its overflow limit only as the attribute it was configured into.
        assert enginePool._max_overflow == database.POOL_MAX_OVERFLOW
        assert (enginePool.checkedin(), enginePool.checkedout()) == (0, 0)
    finally:
        await database.DisposeEngine(engine)
