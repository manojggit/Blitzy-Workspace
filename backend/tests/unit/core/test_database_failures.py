"""Unit tests of how app.core.database and app.core.locks survive a database that goes away.

Replaces: the CICS IOERR handling of the CARDDEMO files of csd/CARDDEMO.CSD, which sends each
program to its WHEN OTHER branch. Covers the Technical Specification, Sections 0.6.5 (WHEN OTHER
literals answer 503, no catch-all handler; asyncpg runs under DB_CONNECT_TIMEOUT_SECONDS and
DB_COMMAND_TIMEOUT_SECONDS) and 0.6.10 (LeaveOnlineWindow discards a connection whose unlock
fails), for QA findings QA2A-F01, QA2B-F02 and QA2C-F01:

- request teardown never replaces the request's outcome. When a session's close or the window
  unlock fails with an error a dropped or terminated connection raises (SQLAlchemy
  InterfaceError, asyncpg InternalClientError, PendingRollbackError), CloseReadSession and
  GetSession invalidate the session and LeaveOnlineWindow discards the connection; the
  program's LegacyMessageError still propagates, and the admission is freed;
- BuildEngine bounds every pooled connection: its driver class is BoundedCloseConnection, whose
  close terminates the connection once its bound passes and, whether the close completes, runs
  out of time or is cancelled, ends by aborting the socket's transport (QAPF2-F01: asyncpg's
  abort does nothing once a graceful close has begun), and its deadline of one connect plus
  one command timeout is an engine execution option that EngineDeadline reads and core.locks
  names alike; BuildSessionFactory makes DeadlineSession sessions, whose explicit connection
  checkout and stream start, as CR00's report snapshot and cursor use them, end at that
  deadline with TimeoutError however long the database leaves them waiting.

No test touches a database: sessions and connections are AsyncMock stand-ins, the engine never
connects, and every key and password is generated at run time with the secrets module.
"""

import asyncio
import contextlib
import secrets
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from sqlalchemy.exc import InterfaceError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.core import database, locks
from app.core.config import Settings
from app.core.errors import LegacyMessageError

HTTP_FILE_ERROR = 503
FILE_ERROR_LITERAL = "Unable to lookup Transaction..."
ACCT_ID_FIELD = "acct_id"
CONNECT_TIMEOUT_SECONDS = 1.5
COMMAND_TIMEOUT_SECONDS = 2.5
SHORT_CLOSE_BOUND_SECONDS = 0.05
# Longer than any test waits, so only a bound ends a close that waits this long.
HUNG_CLOSE_SECONDS = 60.0
# Driver timeouts whose sum, the engine deadline, ends a hung session call within a test.
SHORT_DRIVER_TIMEOUTS = {"DB_CONNECT_TIMEOUT_SECONDS": 0.05, "DB_COMMAND_TIMEOUT_SECONDS": 0.05}
# The DeadlineSession overrides that may start a checkout, besides execute.
DEADLINE_SESSION_METHODS = ("connection", "stream")

# The errors a connection the database dropped or terminated raises on its next operation.
TEARDOWN_FAILURES = (
    pytest.param(
        InterfaceError(
            "ROLLBACK",
            None,
            asyncpg.InterfaceError(
                "cannot call Transaction.rollback(): the underlying connection is closed"
            ),
        ),
        id="interface-error",
    ),
    pytest.param(
        asyncpg.InternalClientError(
            "cannot switch to state 15; another operation (2) is in progress"
        ),
        id="internal-client-error",
    ),
    pytest.param(
        PendingRollbackError("Can't reconnect until invalid transaction is rolled back."),
        id="pending-rollback-error",
    ),
)


def BuildDatabaseUrl(driverName: str, databasePassword: str) -> str:
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://bound_test:{databasePassword}@127.0.0.1/bound_test"


@pytest.fixture(name="boundSettings")
def BoundSettings() -> Settings:
    """Return backend Settings with generated secrets and distinct driver timeouts."""
    databasePassword = secrets.token_urlsafe(12)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(32),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
        DB_CONNECT_TIMEOUT_SECONDS=CONNECT_TIMEOUT_SECONDS,
        DB_COMMAND_TIMEOUT_SECONDS=COMMAND_TIMEOUT_SECONDS,
    )


def FailingSession(closeError: BaseException) -> AsyncMock:
    """Return a request-session stand-in whose close raises closeError."""
    session = AsyncMock(spec=AsyncSession)
    session.close.side_effect = closeError
    return session


def SessionRequest(session: AsyncMock, settings: Settings) -> SimpleNamespace:
    """Return a GET request stand-in whose app's session factory hands out ``session``.

    The app keeps ``settings``, whose DB_CONNECT_TIMEOUT_SECONDS bounds the admission wait.
    """
    appState = SimpleNamespace(sessionFactory=lambda: session, settings=settings)
    return SimpleNamespace(
        app=SimpleNamespace(state=appState), method="GET", state=SimpleNamespace()
    )


def WindowRequest(connection: AsyncMock) -> SimpleNamespace:
    """Return a request stand-in that holds the shared window lock on ``connection``."""
    request = SimpleNamespace(state=SimpleNamespace())
    locks.StoreWindowState(request, locks.WINDOW_GRANTED, connection)
    return request


def UnconnectedBoundedConnection() -> database.BoundedCloseConnection:
    """Return a BoundedCloseConnection that never connected; asyncpg's finalizer sees it closed.

    Its socket transport is a Mock, so a test can see whether the close aborted it.
    """
    connection = object.__new__(database.BoundedCloseConnection)
    connection._aborted = True
    connection._transport = Mock(spec=asyncio.Transport)
    return connection


@pytest.mark.parametrize("closeError", TEARDOWN_FAILURES)
async def test_close_read_session_invalidates_a_session_whose_close_fails(
    closeError: BaseException,
) -> None:
    session = FailingSession(closeError)
    await database.CloseReadSession(session)
    session.invalidate.assert_awaited_once_with()


@pytest.mark.parametrize("closeError", TEARDOWN_FAILURES)
async def test_get_session_keeps_the_request_outcome_when_its_close_fails(
    closeError: BaseException, boundSettings: Settings
) -> None:
    """The service's 503 literal leaves GetSession unchanged; the admission is freed."""
    session = FailingSession(closeError)
    request = SessionRequest(session, boundSettings)
    serviceError = LegacyMessageError(FILE_ERROR_LITERAL, ACCT_ID_FIELD, HTTP_FILE_ERROR)
    with pytest.raises(LegacyMessageError) as raised:
        async with contextlib.asynccontextmanager(database.GetSession)(request) as yielded:
            assert yielded is session
            raise serviceError
    assert raised.value is serviceError
    session.invalidate.assert_awaited_once_with()
    assert database.RequestAdmission(request).value == database.ADMITTED_REQUEST_LIMIT


@pytest.mark.parametrize("unlockError", TEARDOWN_FAILURES)
async def test_leave_window_discards_a_connection_whose_unlock_fails(
    unlockError: BaseException,
) -> None:
    connection = AsyncMock(spec=AsyncConnection)
    connection.execute.side_effect = unlockError
    request = WindowRequest(connection)
    await locks.LeaveOnlineWindow(request)
    connection.invalidate.assert_awaited_once_with()
    connection.close.assert_awaited_once_with()
    assert locks.WindowState(request) is None


@pytest.mark.parametrize(
    ("closeTimeout", "patchedDefault"),
    [(SHORT_CLOSE_BOUND_SECONDS, None), (None, SHORT_CLOSE_BOUND_SECONDS)],
    ids=["given-timeout", "default-bound"],
)
async def test_bounded_close_terminates_a_close_the_server_never_confirms(
    closeTimeout: float | None, patchedDefault: float | None, monkeypatch: Any
) -> None:
    """A graceful close past its bound, the given timeout or GRACEFUL_CLOSE_SECONDS, terminates."""
    terminations: list[object] = []

    async def HungClose(self: object, *, timeout: float | None = None) -> None:
        await asyncio.sleep(HUNG_CLOSE_SECONDS)

    def RecordTermination(self: object) -> None:
        terminations.append(self)

    if patchedDefault is not None:
        monkeypatch.setattr(database, "GRACEFUL_CLOSE_SECONDS", patchedDefault)
    monkeypatch.setattr(asyncpg.Connection, "close", HungClose)
    monkeypatch.setattr(database.BoundedCloseConnection, "terminate", RecordTermination)
    connection = UnconnectedBoundedConnection()
    async with asyncio.timeout(HUNG_CLOSE_SECONDS / 2):
        await connection.close(timeout=closeTimeout)
    assert terminations == [connection]
    connection._transport.abort.assert_called_once_with()


async def test_bounded_close_stays_graceful_when_the_server_confirms(monkeypatch: Any) -> None:
    """A confirmed close terminates nothing; the closing abort of its transport is a no-op."""
    gracefulCloses: list[float | None] = []
    terminations: list[object] = []

    async def ConfirmedClose(self: object, *, timeout: float | None = None) -> None:
        gracefulCloses.append(timeout)

    def RecordTermination(self: object) -> None:
        terminations.append(self)

    monkeypatch.setattr(asyncpg.Connection, "close", ConfirmedClose)
    monkeypatch.setattr(database.BoundedCloseConnection, "terminate", RecordTermination)
    connection = UnconnectedBoundedConnection()
    await connection.close()
    assert (gracefulCloses, terminations) == ([None], [])
    connection._transport.abort.assert_called_once_with()


async def test_bounded_close_cancelled_while_waiting_still_aborts_the_transport(
    monkeypatch: Any,
) -> None:
    """A cancelled close re-raises the cancellation with its socket aborted, never left open."""
    closeStarted = asyncio.Event()

    async def HungClose(self: object, *, timeout: float | None = None) -> None:
        closeStarted.set()
        await asyncio.sleep(HUNG_CLOSE_SECONDS)

    monkeypatch.setattr(asyncpg.Connection, "close", HungClose)
    connection = UnconnectedBoundedConnection()
    closeTask = asyncio.create_task(connection.close())
    await closeStarted.wait()
    closeTask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closeTask
    connection._transport.abort.assert_called_once_with()


async def test_build_engine_bounds_close_and_sets_the_checkout_deadline(
    boundSettings: Settings, monkeypatch: Any
) -> None:
    """Pooled connections are BoundedCloseConnection; the deadline is connect plus command."""
    engineArguments: dict[str, Any] = {}

    def RecordingCreateAsyncEngine(*args: Any, **kwargs: Any) -> Any:
        engineArguments.update(kwargs)
        return create_async_engine(*args, **kwargs)

    monkeypatch.setattr(database, "create_async_engine", RecordingCreateAsyncEngine)
    engine = database.BuildEngine(boundSettings)
    try:
        connectArguments = engineArguments["connect_args"]
        assert connectArguments["connection_class"] is database.BoundedCloseConnection
        assert database.DriverConnectArguments(boundSettings).items() <= connectArguments.items()
        expectedDeadline = CONNECT_TIMEOUT_SECONDS + COMMAND_TIMEOUT_SECONDS
        assert database.EngineDeadline(engine) == expectedDeadline
        assert engine.get_execution_options()[locks.DEADLINE_EXECUTION_OPTION] == expectedDeadline
    finally:
        await database.DisposeEngine(engine)


async def test_engine_deadline_is_absent_from_engines_build_engine_did_not_build(
    boundSettings: Settings,
) -> None:
    engine = create_async_engine(boundSettings.DATABASE_URL)
    try:
        assert (database.EngineDeadline(engine), database.EngineDeadline(None)) == (None, None)
    finally:
        await engine.dispose()


async def test_session_factory_makes_deadline_sessions(boundSettings: Settings) -> None:
    engine = database.BuildEngine(boundSettings)
    try:
        session = database.BuildSessionFactory(engine)()
        assert isinstance(session, database.DeadlineSession)
        await session.close()
    finally:
        await database.DisposeEngine(engine)


@pytest.mark.parametrize("methodName", DEADLINE_SESSION_METHODS)
async def test_deadline_session_checkout_and_stream_expire_at_the_engine_deadline(
    methodName: str, boundSettings: Settings, monkeypatch: Any
) -> None:
    """A call the database never answers ends at the deadline with TimeoutError.

    Without the deadline the hung call would return after HUNG_CLOSE_SECONDS, raising nothing.
    """

    async def HungCall(self: object, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(HUNG_CLOSE_SECONDS)

    monkeypatch.setattr(AsyncSession, methodName, HungCall)
    engine = database.BuildEngine(boundSettings.model_copy(update=dict(SHORT_DRIVER_TIMEOUTS)))
    try:
        session = database.BuildSessionFactory(engine)()
        with pytest.raises(TimeoutError):
            await getattr(session, methodName)()
        await session.close()
    finally:
        await database.DisposeEngine(engine)
