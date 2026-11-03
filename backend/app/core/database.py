"""Async engine, request session and per-record write units of the online programs.

Replaces: CICS file control over the CARDDEMO files of csd/CARDDEMO.CSD (ACCTDAT, CARDDAT,
CUSTDAT, TRANSACT, USRSEC and the CARDAIX, CCXREF and CXACAIX paths) and their RECOVERY(NONE)
durability: each record write commits in its own unit, as a non-recoverable VSAM write is
durable at once. A completed write therefore survives a later error, a lost connection, task
cancellation or the death of the process, just as SYNCPOINT ROLLBACK in COACTUPC
9600-WRITE-PROCESSING undid nothing. Specified in the Technical Specification, Sections
0.6.10 and 0.6.12.

Reads that lock nothing (keyed reads, browses, AIX lookups) run on the request session that
GetSession yields. A read that fails there leaves the session's transaction unusable, so a
service that goes on to read another file first calls RecoverRequestSession. A record's READ
UPDATE and its WRITE, REWRITE or DELETE run inside one WriteUnit, on a pooled connection of its
own:

- CAUP (COACTUPC) opens the CUSTDAT unit and then the ACCTDAT unit, locking the account record
  before the customer record and both before either rewrite; the account unit commits first.
- CB00 (COBIL00C) holds the ACCTDAT unit from its READ UPDATE to the REWRITE, while a separate
  TRANSACT unit allocates the TRAN-ID and inserts the payment.
- CT02 (COTRN02C) and CU01 (COUSR01C) insert in one unit each.
- CCUP (COCRDUPC), CU02 (COUSR02C) and CU03 (COUSR03C) use one unit each.

Units never take the batch-window lock: core.locks.EnterOnlineWindow holds it on a separate
connection for the whole request.

A request therefore holds up to four pooled connections at once: the session, the window
connection and two units. GetSession admits at most ADMITTED_REQUEST_LIMIT requests at a time,
so admitted requests never need more connections than the pool holds, and a request waits for
admission before it holds any. Requests can thus never each hold a connection while waiting for
another until the pool timeout. The pool closes a returned connection beyond POOL_SIZE, so
POOL_SIZE covers all the connections the admitted requests can hold at once and only
POOL_HEADROOM_CONNECTIONS can overflow. Steady load at any concurrency therefore reuses warm
connections rather than opening new PostgreSQL sessions.

CR00 (CORPT00C) takes the admission only for its reads. Its route depends on GetReportSession,
which admits nothing, and corpt00c runs the three report reads inside AdmittedReads, which ends
the read transaction and returns the session's connection before it frees the admission. The
report is then built and rendered holding neither, so a queued or running report never keeps
other online requests waiting. Its reads hold one connection, within REQUEST_CONNECTION_LIMIT,
and corpt00c takes its report slot before the admission, never while holding one, so the
reasoning above still holds. Every other route is admitted by GetSession as described above.

main.CreateApp keeps the settings, the engine and the session factory on app.state, and
everything here reads them from there, so tests can swap engines. The admission semaphore is
added to app.state by the first request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from anyio import CancelScope, Semaphore
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import ParseDatabaseUrl, Settings
from app.core.errors import FileIoError, MapDatabaseError, RepositoryError
from app.core.logging import SuppressEngineStatementLogging

# The first statement of every unit: a SELECT ... FOR UPDATE waits at most 5 seconds for a
# record another task holds, then fails with SQLSTATE 55P03 (RecordLockError).
WRITE_UNIT_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"

# The most pooled connections one request holds at once: the request session (from its first
# read to the end of the request), the batch-window connection and, in CAUP and CB00, two
# nested write units.
REQUEST_CONNECTION_LIMIT = 4
# The most connections the engine has open at once, kept and overflow together.
POOL_CAPACITY = 40
# Connections no admitted request counts on: a cancelled request's session can still be closing
# after its admission is freed.
POOL_HEADROOM_CONNECTIONS = 4
# GetSession admits at most this many requests at once, and a request waits for admission
# before it holds any connection. All admitted requests together need at most
# POOL_CAPACITY - POOL_HEADROOM_CONNECTIONS connections, so an admitted request's checkout waits
# only for a connection that is free or that its holder returns without waiting for another.
# No cycle of requests each holding a connection while waiting for the next can form, however
# many requests arrive, so none of them stalls until the pool timeout.
ADMITTED_REQUEST_LIMIT = (POOL_CAPACITY - POOL_HEADROOM_CONNECTIONS) // REQUEST_CONNECTION_LIMIT
# The pool keeps open every connection the admitted requests can hold at once. It closes a
# returned connection beyond POOL_SIZE, so a smaller pool would open and close PostgreSQL
# sessions under steady load, because each window-gated request holds two connections.
# Overflow covers only the headroom for sessions still closing after their admission is freed.
POOL_SIZE = ADMITTED_REQUEST_LIMIT * REQUEST_CONNECTION_LIMIT
POOL_MAX_OVERFLOW = POOL_CAPACITY - POOL_SIZE
# The app.state attribute of the admission semaphore, created by the first GetSession call.
REQUEST_ADMISSION_ATTRIBUTE = "requestAdmission"

# Failures a unit can meet besides a wrapped DBAPIError, all reported as CICS IOERR. The
# asyncpg dialect raises connect-time rejections unwrapped (database starting up 57P03, too
# many connections 53300, unknown database, bad credentials); refused and timed-out connects
# are OSError; a pool checkout that waits past pool_timeout raises PoolTimeoutError.
CONNECTION_FAILURES = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, PoolTimeoutError)
UNIT_FAILURES = (DBAPIError, *CONNECTION_FAILURES)


def DriverConnectArguments(settings: Settings) -> dict[str, float]:
    """Return asyncpg's connect timeout (``timeout``) and per-statement ``command_timeout``.

    The one source of the driver timeouts, for the engine's pooled connections and for the
    readiness probe's own connection.
    """
    return {
        "timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
        "command_timeout": settings.DB_COMMAND_TIMEOUT_SECONDS,
    }


def BuildEngine(settings: Settings) -> AsyncEngine:
    """Build the asyncpg engine of DATABASE_URL without connecting.

    Every pooled connection gets the driver timeouts of DriverConnectArguments. Connections
    open on first use, so the backend starts while PostgreSQL is down and GET /health/ready
    reports 503 until the database is reachable. A DATABASE_URL that does not parse raises
    ParseDatabaseUrl's ValueError, which never repeats the URL.

    Once opened, up to POOL_SIZE connections, all that the admitted requests can hold at once,
    stay in the pool between requests, so steady load reuses them. POOL_MAX_OVERFLOW more,
    the closing-session headroom, are closed when returned; POOL_CAPACITY bounds the total.

    Password hashes, SSNs, CVVs and customer data travel as bound parameters and result rows,
    so hide_parameters=True keeps parameters out of SQLAlchemy's error text, and
    SuppressEngineStatementLogging stops statement, parameter and row log records from being
    created at all.
    """
    SuppressEngineStatementLogging()
    return create_async_engine(
        ParseDatabaseUrl(settings.DATABASE_URL, "DATABASE_URL"),
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        connect_args=DriverConnectArguments(settings),
        hide_parameters=True,
    )


def BuildSessionFactory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the factory of request sessions; loaded rows stay readable after a commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def DisposeEngine(engine: AsyncEngine) -> None:
    """Close every pooled connection of the engine; the application lifespan calls it."""
    await engine.dispose()


def RequestAdmission(request: Request) -> Semaphore:
    """Return the application's admission semaphore of ADMITTED_REQUEST_LIMIT permits.

    The first call creates it on app.state, beside the engine. Nothing is awaited between the
    lookup and the store, so concurrent first requests on the event loop share one semaphore.
    """
    admission: Semaphore | None = getattr(request.app.state, REQUEST_ADMISSION_ATTRIBUTE, None)
    if admission is None:
        admission = Semaphore(ADMITTED_REQUEST_LIMIT)
        setattr(request.app.state, REQUEST_ADMISSION_ATTRIBUTE, admission)
    return admission


async def GetSession(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the request's session, for reads that lock nothing, once the request is admitted.

    The request first waits, in arrival order, for one of the ADMITTED_REQUEST_LIMIT
    admissions, before it holds any pooled connection. Its batch-window connection and write
    units are taken and returned while it is admitted, because every online context depends on
    this dependency and ends first. The session never commits and nothing writes through it:
    every write, the login throttle's included, runs in a WriteUnit. Leaving the request rolls
    back the read-only transaction, returns the connection to the pool and then frees the
    admission, whether the request succeeds, fails or is cancelled; when a repeated
    cancellation interrupts the close, the close finishes in the background, within
    POOL_HEADROOM_CONNECTIONS. Tests replace this dependency, admission included, through
    ``app.dependency_overrides[GetSession]``.
    """
    sessionFactory: async_sessionmaker[AsyncSession] = request.app.state.sessionFactory
    async with RequestAdmission(request), sessionFactory() as session:
        yield session


async def GetReportSession(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the CR00 request's session without admitting the request; close it at the end.

    The session holds no connection until its first read, and the CR00 service runs every read
    inside AdmittedReads, which takes the admission only for those reads. Leaving the request
    closes the session, a no-op once AdmittedReads has closed it.
    """
    sessionFactory: async_sessionmaker[AsyncSession] = request.app.state.sessionFactory
    async with sessionFactory() as session:
        yield session


async def CloseReadSession(session: AsyncSession) -> None:
    """Roll back and close a request session that only read, returning its connection.

    When the close fails on a broken connection, the session is invalidated instead, so the
    driver connection is discarded rather than pooled. The failure is not raised, so the
    caller keeps the outcome of its reads.
    """
    try:
        await session.close()
    except UNIT_FAILURES:
        await session.invalidate()


@asynccontextmanager
async def AdmittedReads(request: Request, session: AsyncSession) -> AsyncIterator[None]:
    """Run a block of reads on ``session`` under one of the request admissions.

    The block waits, in arrival order, for one of the ADMITTED_REQUEST_LIMIT admissions before
    its first read. At exit, whether the block returns, raises or is cancelled, the session's
    read transaction is ended and its connection returned to the pool before the admission is
    freed, so a request that goes on working after its reads holds neither. The close is
    shielded, because under a cancellation every await would be cancelled again.
    """
    async with RequestAdmission(request):
        try:
            yield
        finally:
            with CancelScope(shield=True):
                await CloseReadSession(session)


def TranslateWriteFailure(exc: Exception, fileName: str) -> RepositoryError:
    """Return the CICS file condition that a unit failure gives the legacy program.

    A wrapped database error maps by its SQLSTATE: 23505 is DuplicateKeyError, 55P03 is
    RecordLockError and any other is FileIoError. A connection-level failure is FileIoError.
    """
    if isinstance(exc, DBAPIError):
        return MapDatabaseError(exc, fileName)
    return FileIoError(fileName)


async def DiscardUnitTransaction(connection: AsyncConnection) -> None:
    """Roll back a unit that did not commit, discarding its connection if the rollback fails.

    Invalidating closes the driver connection instead of returning it to the pool, so
    PostgreSQL ends the transaction and frees its row locks. The rollback failure is not
    raised, so the caller sees the exception that ended the unit: a legacy error, a database
    error or a task cancellation.
    """
    try:
        await connection.rollback()
    except UNIT_FAILURES:
        await connection.invalidate()


async def RecoverRequestSession(session: AsyncSession) -> None:
    """End the request session's read-only transaction after a read on it failed.

    A SELECT that fails on the server aborts the transaction, and PostgreSQL refuses every
    later statement in it with SQLSTATE 25P02; a dropped connection leaves the session pending
    rollback. A service whose legacy program reads another file after such a failure calls
    this first, because CICS files fail independently. The session never writes, so the
    rollback discards nothing, and the batch-window lock stays held on its own connection
    (core.locks). When the rollback itself fails, the session is invalidated, so its next read
    opens a fresh connection. The rollback failure is not raised, so the caller keeps the file
    condition of the read that failed.
    """
    try:
        await session.rollback()
    except UNIT_FAILURES:
        await session.invalidate()


@asynccontextmanager
async def WriteUnit(engine: AsyncEngine, fileName: str) -> AsyncIterator[AsyncConnection]:
    """Run one record's READ UPDATE and write in a transaction of its own, committed at exit.

    ``fileName`` is the legacy CICS file (ACCTDAT, CUSTDAT, CARDDAT, TRANSACT or USRSEC) named
    by the raised file condition. The repository issues the ``SELECT ... FOR UPDATE`` and the
    single INSERT, UPDATE or DELETE on the yielded connection, with bound parameters only, and
    never commits. A clean exit commits at once, so the write is durable before the next write
    starts. On an exception the transaction is rolled back, and then:

    - a RepositoryError or LegacyMessageError raised by the body, and a task cancellation,
      propagate unchanged;
    - a DBAPIError from connecting, BEGIN, the body or COMMIT becomes the MapDatabaseError
      condition (23505 DuplicateKeyError, 55P03 RecordLockError, other FileIoError);
    - a connection-level failure (CONNECTION_FAILURES) becomes FileIoError.

    The transaction is ended explicitly rather than by ``connection.begin()`` used as a context
    manager, because there a failed rollback would replace the body's own exception.
    """
    try:
        async with engine.connect() as connection:
            await connection.begin()
            await connection.execute(text(WRITE_UNIT_LOCK_TIMEOUT_SQL))
            committed = False
            try:
                yield connection
                await connection.commit()
                committed = True
            finally:
                if not committed:
                    await DiscardUnitTransaction(connection)
    except UNIT_FAILURES as exc:
        raise TranslateWriteFailure(exc, fileName) from exc
