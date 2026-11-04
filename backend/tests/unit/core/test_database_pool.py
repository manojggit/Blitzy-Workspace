"""Unit tests for the connection-pool sizing of app.core.database.

The unit replaces CICS file control over the CARDDEMO files of csd/CARDDEMO.CSD, as specified in
the Technical Specification, Sections 0.6.10 and 0.6.12: a request holds up to four pooled
connections (its session, the batch-window connection and two write units), and GetSession
admits at most ADMITTED_REQUEST_LIMIT requests at once. These tests pin that the pool keeps
every connection the admitted requests can hold at once, so steady load reuses warm connections
instead of opening and closing overflow ones, that only the closing-session headroom overflows,
that admission still leaves that headroom within the pool's capacity, that requests which can
change data (POST, PUT, DELETE) are admitted at most MUTATING_REQUEST_LIMIT at a time, leaving
RESERVED_READ_ADMISSIONS of the admissions to every other request, and that BuildEngine
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
# Six of them at most may change data, so three always stay for reads and DB-free requests.
EXPECTED_MUTATING_REQUEST_LIMIT = 6
EXPECTED_RESERVED_READ_ADMISSIONS = 3
EXPECTED_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE"})
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
        (operator.eq, database.MUTATING_REQUEST_LIMIT, EXPECTED_MUTATING_REQUEST_LIMIT),
        (operator.eq, database.RESERVED_READ_ADMISSIONS, EXPECTED_RESERVED_READ_ADMISSIONS),
        (
            operator.eq,
            database.MUTATING_REQUEST_LIMIT + database.RESERVED_READ_ADMISSIONS,
            database.ADMITTED_REQUEST_LIMIT,
        ),
        (operator.lt, database.MUTATING_REQUEST_LIMIT, database.ADMITTED_REQUEST_LIMIT),
        (operator.eq, database.MUTATING_METHODS, EXPECTED_MUTATING_METHODS),
    ],
    ids=[
        "admitted-peak-stays-pooled",
        "admitted-peak-plus-headroom-is-capacity",
        "pool-plus-overflow-is-capacity",
        "only-headroom-overflows",
        "admission-limit-unchanged",
        "mutating-limit",
        "reserved-read-admissions",
        "mutating-plus-reserved-is-admission-limit",
        "mutating-requests-never-take-every-admission",
        "mutating-methods",
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
