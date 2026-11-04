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

Requests that can change data (POST, PUT, DELETE) wait on locks or on bcrypt while admitted,
so GetSession first makes each of them wait, holding nothing, for one of MUTATING_REQUEST_LIMIT
permits, which leaves RESERVED_READ_ADMISSIONS admissions to every other request. A request
waits for its permit and its admission for at most DB_CONNECT_TIMEOUT_SECONDS in all, since
waiting for admission is waiting for the capacity to open a connection. A request not admitted
in time is refused: it holds no admission, and its session, window connection and write units
come from the refusing engine (RefusingEngine), which fails every connection at once with
AdmissionTimeoutError, an IOERR to the repositories. Its program answers its WHEN OTHER literal
at once, and a refused request never checks out a pooled connection, so the reasoning above
holds. GET /auth/me keeps its blank display names.

CR00 (CORPT00C) takes the admission only for its reads and its build. Its route depends on
GetReportSession, which admits nothing, and corpt00c runs the report's reads, and builds it from
a second read of the same snapshot, inside AdmittedReads, which ends the read transaction and
returns the session's connection before it frees the admission. Both are freed the moment the
build ends, while the response may still be sending the built document, so a queued report or
a client still reading one never keeps other online requests waiting. The report holds one
connection, within REQUEST_CONNECTION_LIMIT, and corpt00c takes its report slot before the
admission, never while holding one, so the reasoning above still holds. AdmittedReads waits
for the admission within the same bound, and a refusal is an IOERR of the first file the reads
address. The menus CM00 and CA00, whose programs do no file I/O, depend on
GetUnadmittedSession, which admits nothing and whose session and request engine are the
refusing engine's. Every other route is admitted by GetSession as described above.

A database that accepts connections but stops answering never holds a request for the
outage. Every pooled driver connection is a BoundedCloseConnection, so discarding one waits at
most GRACEFUL_CLOSE_SECONDS. Each checkout (WriteUnit's, the window connection's and the login
throttle's included) and each DeadlineSession statement, stream start and explicit connection
checkout ends within EngineDeadline, that is DB_CONNECT_TIMEOUT_SECONDS plus
DB_COMMAND_TIMEOUT_SECONDS. The expiry is a TimeoutError, which
the repositories report as IOERR, so each program answers its WHEN OTHER literal. A request
queued behind admitted requests that wait out that deadline is refused after the admission
bound instead. A database that drops its connections mid-request leaves that literal standing
too: GetSession closes the session through CloseReadSession, which invalidates a session whose
close fails, and core.locks.LeaveOnlineWindow discards a window connection whose unlock fails.

main.CreateApp keeps the settings, the engine and the session factory on app.state, and
everything here reads them from there, so tests can swap engines. The admission semaphores and
the refusing engine are added to app.state by the first request that needs them.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import asyncpg
from anyio import CancelScope, Semaphore, fail_after, move_on_after
from fastapi import Request
from sqlalchemy import Result, text
from sqlalchemy.exc import DBAPIError, PendingRollbackError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncResult,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import ParseDatabaseUrl, Settings
from app.core.errors import FileIoError, MapDatabaseError, RepositoryError
from app.core.locks import REQUEST_ENGINE_ATTRIBUTE
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
# Connections no admitted request counts on: a write unit's close that a repeated cancellation
# interrupts finishes in the background, possibly after its request's admission is freed.
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
# Overflow covers only the headroom for connections still closing after admission is freed.
POOL_SIZE = ADMITTED_REQUEST_LIMIT * REQUEST_CONNECTION_LIMIT
POOL_MAX_OVERFLOW = POOL_CAPACITY - POOL_SIZE
# The HTTP methods of requests that can change data. Admitted, these wait on locks or on the
# CPU: the sign-on on its bcrypt check and throttle writes, CT02 and CB00 on the TRAN-ID
# allocation lock, CAUP, CCUP and CU01-CU03 on row locks for up to their lock timeout. The
# read-only selection POSTs share their class, which only makes them share its permits.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE"})
# Admissions only requests of other methods can hold, so quick reads are never queued behind a
# burst of sign-ons or of lock-blocked writes that hold every admission.
RESERVED_READ_ADMISSIONS = 3
# GetSession makes a request of MUTATING_METHODS wait for one of these permits, holding nothing,
# before it waits for an admission, so at most this many of them are admitted at once. Both
# waits share one bound, and every request takes this permit before the admission and never
# the other way round, so no request holds what another waits for while it waits itself.
MUTATING_REQUEST_LIMIT = ADMITTED_REQUEST_LIMIT - RESERVED_READ_ADMISSIONS
# The app.state attribute of the admission semaphore, created by the first GetSession call.
REQUEST_ADMISSION_ATTRIBUTE = "requestAdmission"
# The app.state attribute of the semaphore of MUTATING_REQUEST_LIMIT permits.
MUTATING_ADMISSION_ATTRIBUTE = "mutatingRequestAdmission"
# The app.state attribute of the refusing engine, created by the first request that needs it.
REFUSING_ENGINE_ATTRIBUTE = "refusingEngine"
# The text of the AdmissionTimeoutError that every connection of a refused request raises.
ADMISSION_REFUSED_TEXT = "no request admission came free within DB_CONNECT_TIMEOUT_SECONDS"

# Failures a unit can meet besides a wrapped DBAPIError, all reported as CICS IOERR. The
# asyncpg dialect raises connect-time rejections unwrapped (database starting up 57P03, too
# many connections 53300, unknown database, bad credentials); refused and timed-out connects,
# and the AdmissionTimeoutError of a request refused admission, are OSError; a pool checkout
# that waits past pool_timeout raises PoolTimeoutError. asyncpg raises InternalClientError,
# which the dialect leaves untranslated, when PostgreSQL ends the session mid-operation (server
# shutdown, pg_terminate_backend): "cannot switch to state ...; another operation is in
# progress". A statement on a connection that a lost connection invalidated inside its
# transaction raises PendingRollbackError, as CB00's WRITE does after its TRAN-ID browse lost
# the TRANSACT unit's connection.
CONNECTION_FAILURES = (
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    PendingRollbackError,
    PoolTimeoutError,
)
UNIT_FAILURES = (DBAPIError, *CONNECTION_FAILURES)

# The longest a graceful close of a pooled driver connection waits for the server before the
# socket is aborted instead: the bound SQLAlchemy gives the close of an invalidated asyncpg
# connection.
GRACEFUL_CLOSE_SECONDS = 2.0
# The engine execution option, set by BuildEngine, holding the seconds one connection checkout
# and one statement may take together: DB_CONNECT_TIMEOUT_SECONDS for opening a connection plus
# DB_COMMAND_TIMEOUT_SECONDS for its pre-ping or statement. core.locks keeps a copy of the name.
DEADLINE_EXECUTION_OPTION = "carddemo_deadline_seconds"


class AdmissionTimeoutError(TimeoutError):
    """The request was refused: no admission came free within DB_CONNECT_TIMEOUT_SECONDS.

    Waiting for admission is waiting for the capacity to open a connection, so the wait has the
    connect bound. Every connection a refused request asks for raises this at once. It is a
    TimeoutError, so an OSError and one of the CONNECTION_FAILURES and of core.locks
    WINDOW_FAILURES: the repositories, WriteUnit, the batch window and the login throttle
    report it as IOERR, and each program answers its WHEN OTHER literal.
    """


class BoundedCloseConnection(asyncpg.Connection):
    """The driver connection of every pooled connection, whose graceful close is bounded.

    asyncpg's close first waits, with no bound, for the server to take any pending cancel
    request, such as the one a statement past command_timeout leaves. A database that stops
    answering never takes it, so every discard or close of that connection, the pool's
    invalidation after a failed pre-ping included, would wait for the whole outage. Here the
    close waits at most ``timeout`` seconds, or GRACEFUL_CLOSE_SECONDS when none is given, and
    then terminates the connection: the socket is aborted, and PostgreSQL ends the session and
    its locks when it notices. A close the server confirms in time stays graceful.

    asyncpg's own abort does nothing once its graceful close has begun, so a close interrupted
    while it waits, by the bound or by a cancellation, would leave the socket open: the
    connection would be gone from the pool while its server session lived on, idle or idle in
    transaction, until the process ended. The close therefore always ends by aborting the
    connection's transport, which is a no-op after a close the server confirmed.
    """

    # SQLAlchemy's asyncpg adapter calls this exact name, so it stays lower-case: a
    # framework-mandated exception to the MK_Modernization Rule's PascalCase method naming.
    async def close(self, *, timeout: float | None = None) -> None:
        """Close gracefully within the bound; past it, terminate the connection instead."""
        closeBound = GRACEFUL_CLOSE_SECONDS if timeout is None else timeout
        try:
            async with asyncio.timeout(closeBound):
                await super().close(timeout=timeout)
        except TimeoutError:
            self.terminate()
        finally:
            # asyncpg keeps the socket's transport only as this attribute.
            self._transport.abort()


def EngineDeadline(bind: AsyncEngine | AsyncConnection | None) -> float | None:
    """Return the DEADLINE_EXECUTION_OPTION seconds of ``bind``, or None for no deadline.

    An engine that BuildEngine did not build, such as a test's unpooled engine, carries no
    deadline, and ``anyio.fail_after(None)`` never expires.
    """
    if bind is None:
        return None
    return bind.get_execution_options().get(DEADLINE_EXECUTION_OPTION)


class DeadlineSession(AsyncSession):
    """The request session: each statement, with the checkout it may start, has a deadline.

    The deadline is EngineDeadline of the session's engine. It bounds each statement, the
    start of each streamed result (CR00's server-side cursor) and each explicit checkout of
    the session's connection (CR00's report snapshot), all of which may start a checkout and
    its pre-ping. A pre-ping whose rollback awaits a cancel request an unresponsive database
    never takes is not bounded by the driver timeouts, so the deadline is what ends it. Its
    expiry raises TimeoutError, an OSError and so one of the CONNECTION_FAILURES, which the
    repositories report as IOERR. The rows a streamed result fetches later need no deadline:
    each asyncpg cursor fetch runs under DB_COMMAND_TIMEOUT_SECONDS, and the connection that
    a fetch past it leaves is discarded within GRACEFUL_CLOSE_SECONDS (BoundedCloseConnection).
    """

    # It overrides AsyncSession.execute, which the repositories call by this exact name, so it
    # stays lower-case: a framework-mandated exception to the MK_Modernization Rule's PascalCase
    # method naming.
    async def execute(self, *args: Any, **kwargs: Any) -> Result[Any]:
        """Run AsyncSession.execute within the deadline of the session's engine."""
        with fail_after(EngineDeadline(self.bind)):
            return await super().execute(*args, **kwargs)

    # It overrides AsyncSession.connection, which the CR00 report snapshot calls by this exact
    # name, so it stays lower-case: a framework-mandated exception to the MK_Modernization
    # Rule's PascalCase method naming.
    async def connection(self, *args: Any, **kwargs: Any) -> AsyncConnection:
        """Run AsyncSession.connection, its checkout and pre-ping included, within the deadline."""
        with fail_after(EngineDeadline(self.bind)):
            return await super().connection(*args, **kwargs)

    # It overrides AsyncSession.stream, which the CR00 report passes call by this exact name, so
    # it stays lower-case: a framework-mandated exception to the MK_Modernization Rule's
    # PascalCase method naming.
    async def stream(self, *args: Any, **kwargs: Any) -> AsyncResult[Any]:
        """Open AsyncSession.stream's result, checkout and cursor included, within the deadline."""
        with fail_after(EngineDeadline(self.bind)):
            return await super().stream(*args, **kwargs)


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

    Every pooled connection gets the driver timeouts of DriverConnectArguments and is a
    BoundedCloseConnection, so closing or discarding it never waits on an unresponsive
    database past GRACEFUL_CLOSE_SECONDS. The engine's DEADLINE_EXECUTION_OPTION, the sum of
    the two driver timeouts, bounds every checkout and request-session statement as a whole
    (EngineDeadline). Connections open on first use, so the backend starts while PostgreSQL is
    down and GET /health/ready reports 503 until the database is reachable. A DATABASE_URL
    that does not parse raises ParseDatabaseUrl's ValueError, which never repeats the URL.

    Once opened, up to POOL_SIZE connections, all that the admitted requests can hold at once,
    stay in the pool between requests, so steady load reuses them. POOL_MAX_OVERFLOW more,
    the closing-connection headroom, are closed when returned; POOL_CAPACITY bounds the total.

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
        connect_args={
            **DriverConnectArguments(settings),
            "connection_class": BoundedCloseConnection,
        },
        execution_options={
            DEADLINE_EXECUTION_OPTION: settings.DB_CONNECT_TIMEOUT_SECONDS
            + settings.DB_COMMAND_TIMEOUT_SECONDS
        },
        hide_parameters=True,
    )


def BuildSessionFactory(engine: AsyncEngine) -> async_sessionmaker[DeadlineSession]:
    """Build the factory of DeadlineSession request sessions; loaded rows outlive a commit."""
    return async_sessionmaker(engine, class_=DeadlineSession, expire_on_commit=False)


async def DisposeEngine(engine: AsyncEngine) -> None:
    """Close every pooled connection of the engine; the application lifespan calls it."""
    await engine.dispose()


def ApplicationSemaphore(request: Request, attributeName: str, permits: int) -> Semaphore:
    """Return the app.state semaphore named ``attributeName``, of ``permits`` permits.

    The first call creates it on app.state, beside the engine. Nothing is awaited between the
    lookup and the store, so concurrent first requests on the event loop share one semaphore.
    """
    semaphore: Semaphore | None = getattr(request.app.state, attributeName, None)
    if semaphore is None:
        semaphore = Semaphore(permits)
        setattr(request.app.state, attributeName, semaphore)
    return semaphore


def RequestAdmission(request: Request) -> Semaphore:
    """Return the application's admission semaphore of ADMITTED_REQUEST_LIMIT permits."""
    return ApplicationSemaphore(request, REQUEST_ADMISSION_ATTRIBUTE, ADMITTED_REQUEST_LIMIT)


def MutatingRequestAdmission(request: Request) -> Semaphore:
    """Return the application's semaphore of MUTATING_REQUEST_LIMIT permits."""
    return ApplicationSemaphore(request, MUTATING_ADMISSION_ATTRIBUTE, MUTATING_REQUEST_LIMIT)


def AdmissionsOf(request: Request) -> tuple[Semaphore, ...]:
    """Return the admissions GetSession takes for the request, in the order it takes them.

    A request of MUTATING_METHODS takes a MutatingRequestAdmission permit and then the request
    admission, so RESERVED_READ_ADMISSIONS stay for the others; any other request takes the
    request admission alone.
    """
    if request.method in MUTATING_METHODS:
        return (MutatingRequestAdmission(request), RequestAdmission(request))
    return (RequestAdmission(request),)


def AdmissionWaitSeconds(request: Request) -> float:
    """Return how long a request waits for admission: DB_CONNECT_TIMEOUT_SECONDS of app.state.

    Waiting for admission is waiting for the capacity to open a connection, so it has the
    connect bound.
    """
    return request.app.state.settings.DB_CONNECT_TIMEOUT_SECONDS


async def AwaitAdmissions(
    request: Request, admissions: Sequence[Semaphore], admissionStack: AsyncExitStack
) -> bool:
    """Take each admission in order within one AdmissionWaitSeconds; return whether all came.

    Each one taken is entered on ``admissionStack``, which frees them in reverse order. When the
    wait expires, the admissions already taken are freed at once, so a refused request holds
    none; a permit granted just as the wait expires goes back to its semaphore.
    """
    with move_on_after(AdmissionWaitSeconds(request)):
        for admission in admissions:
            await admissionStack.enter_async_context(admission)
        return True
    await admissionStack.aclose()
    return False


async def RefuseConnection() -> asyncpg.Connection:
    """Open nothing and raise AdmissionTimeoutError: the refusing engine's connection creator."""
    raise AdmissionTimeoutError(ADMISSION_REFUSED_TEXT)


def RefusingEngine(request: Request) -> AsyncEngine:
    """Return the application's refusing engine, which fails every connection at once.

    It has the dialect of ``request.app.state.engine``, no pool (NullPool) and RefuseConnection
    as its creator, so whatever is bound to it opens no socket, checks out no pooled connection
    and never waits on the database. The first call creates it on app.state; nothing is awaited
    between the lookup and the store, so concurrent first calls share one engine.
    """
    refusingEngine = getattr(request.app.state, REFUSING_ENGINE_ATTRIBUTE, None)
    if refusingEngine is None:
        refusingEngine = create_async_engine(
            request.app.state.engine.url, poolclass=NullPool, async_creator=RefuseConnection
        )
        setattr(request.app.state, REFUSING_ENGINE_ATTRIBUTE, refusingEngine)
    return refusingEngine


def RefusedSession(request: Request) -> AsyncSession:
    """Return a session bound to the refusing engine, which also becomes the request's engine.

    The session's statements, and the window connection and write units the request takes
    through core.locks.RequestEngine, all raise AdmissionTimeoutError at once.
    """
    refusingEngine = RefusingEngine(request)
    setattr(request.state, REQUEST_ENGINE_ATTRIBUTE, refusingEngine)
    sessionFactory: async_sessionmaker[AsyncSession] = request.app.state.sessionFactory
    return sessionFactory(bind=refusingEngine)


async def GetSession(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the request's session, for reads that lock nothing, once the request is admitted.

    The request first waits, in arrival order, for its admissions (AdmissionsOf), before it
    holds any pooled connection, and for at most AdmissionWaitSeconds in all. Every online
    context but CR00's (GetReportSession) and the menus' (GetUnadmittedSession) depends on this
    dependency and ends first, so its batch-window connection and write units are taken and
    returned while it is admitted. A request not admitted in time is refused: it holds no
    admission, and its RefusedSession, window connection and write units fail at once, so its
    program answers its WHEN OTHER literal and it never waits for an outage. The session never
    commits and nothing writes through it: every write, the login throttle's included, runs in
    a WriteUnit. Leaving the request rolls back the read-only transaction, returns the
    connection to the pool and then frees the admissions in reverse order, whether the request
    succeeds, fails or is cancelled. The close is shielded, so even a repeated cancellation
    lets it finish first. It runs through CloseReadSession, so a close that fails on a
    connection the database dropped invalidates the session instead, and the request's own
    outcome is never replaced by the close's error. Tests replace this dependency, admission
    included, through ``app.dependency_overrides[GetSession]``.
    """
    async with AsyncExitStack() as admissionStack:
        if await AwaitAdmissions(request, AdmissionsOf(request), admissionStack):
            session = request.app.state.sessionFactory()
        else:
            session = RefusedSession(request)
        try:
            yield session
        finally:
            with CancelScope(shield=True):
                await CloseReadSession(session)


async def GetUnadmittedSession(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the session of a route whose program does no file I/O, without admitting it.

    CM00 (COMEN01C) and CA00 (COADM01C) read and write no file, so they never wait for an
    admission, behind slow requests or during an outage. Their session is a RefusedSession,
    whose engine is also the request's, so a statement, window connection or write unit the
    program never issues would fail at once as IOERR rather than check out a pooled connection
    unadmitted. Leaving the request closes the session, which holds no connection.
    """
    session = RefusedSession(request)
    try:
        yield session
    finally:
        with CancelScope(shield=True):
            await CloseReadSession(session)


async def GetReportSession(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the CR00 request's session without admitting the request; close it at the end.

    The session holds no connection until its first read, and the CR00 service runs every read
    inside AdmittedReads, which takes the admission only for those reads and the report's
    build. Leaving the request closes the session, a no-op once AdmittedReads has closed it.
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
async def AdmittedReads(
    request: Request, session: AsyncSession, fileName: str
) -> AsyncIterator[None]:
    """Run a block of reads on ``session`` under one of the request admissions.

    The block waits, in arrival order, for one of the ADMITTED_REQUEST_LIMIT admissions before
    its first read, for at most AdmissionWaitSeconds. Past that the request is refused: the
    block never runs, and FileIoError of ``fileName``, the legacy file the block reads first,
    is raised from AdmissionTimeoutError, as a connection that cannot be opened is IOERR. At
    exit, whether the block returns, raises or is cancelled, the session's read transaction is
    ended and its connection returned to the pool before the admission is freed, so a request
    that goes on working after its reads holds neither. The close is shielded, because under a
    cancellation every await would be cancelled again.
    """
    async with AsyncExitStack() as admissionStack:
        if not await AwaitAdmissions(request, (RequestAdmission(request),), admissionStack):
            raise FileIoError(fileName) from AdmissionTimeoutError(ADMISSION_REFUSED_TEXT)
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

    The checkout, its pre-ping, BEGIN and the lock timeout run within EngineDeadline of the
    engine, whose expiry is a TimeoutError and so FileIoError; the body has no such deadline.
    The transaction is ended explicitly rather than by ``connection.begin()`` used as a context
    manager, because there a failed rollback would replace the body's own exception.
    """
    try:
        async with AsyncExitStack() as unitStack:
            with fail_after(EngineDeadline(engine)):
                connection = await unitStack.enter_async_context(engine.connect())
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
