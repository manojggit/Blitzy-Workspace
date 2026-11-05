"""Unit tests of the bounded request admission and the refusal of app.core.database.

Replaces: the CICS region's task admission over the CARDDEMO files of csd/CARDDEMO.CSD, whose
programs take their WHEN OTHER branch when a file cannot be reached. Covers the Technical
Specification, Sections 0.6.5 (database access bounded by DB_CONNECT_TIMEOUT_SECONDS; WHEN
OTHER literals answer 503) and 0.6.13 (bounded database timeouts), for QA findings QAPF2-F01
and QAPF2-F02:

- GetSession makes a POST, PUT or DELETE request wait for one of MUTATING_REQUEST_LIMIT
  permits and then for an admission, any other request for an admission only, and all within
  DB_CONNECT_TIMEOUT_SECONDS. With every permit held by requests that can change data, a GET is
  still admitted. A request not admitted in time is refused holding no permit;
- a refused request's session, its batch-window connection, its write units and the login
  throttle's check-slot connection all fail at once, AdmissionTimeoutError or the IOERR
  condition built from it, and nothing connects to the database;
- GetUnadmittedSession, the menus' session, waits for no admission and never connects either.

No test touches a database: the engine of each app stand-in never connects, asyncpg.connect is
replaced by a recorder, and every key and password is generated with the secrets module.
"""

import contextlib
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from anyio import Semaphore
from sqlalchemy import text

from app.core import database, locks
from app.core.config import Settings
from app.core.dependencies import OnlineContext
from app.core.errors import FileIoError
from app.services.auth import login_throttle

# The admission bound of every app stand-in, and how long after it a refusal may still arrive.
ADMISSION_WAIT_SECONDS = 0.5
REFUSAL_MARGIN_SECONDS = 1.0
# How early the event loop's timer may fire against time.monotonic().
CLOCK_TOLERANCE_SECONDS = 0.01
# Half the admission bound: what an admission, or a failure that waits for nothing, takes.
AT_ONCE_SECONDS = ADMISSION_WAIT_SECONDS / 2
PROBE_STATEMENT = text("SELECT 1")
TRANSACT_FILE_NAME = "TRANSACT"
ACCOUNT_FILE_NAME = "ACCTDAT"
NO_PERMITS = 0


@dataclass(frozen=True)
class AdmissionCase:
    """A request method, the permits other requests hold, and the free permits seen inside.

    ``heldAdmissions`` and ``heldMutatingPermits`` are held while the request enters
    GetSession; ``freeInside`` is (free admissions, free mutating permits) while it is inside.
    """

    method: str
    heldAdmissions: int
    heldMutatingPermits: int
    admitted: bool
    freeInside: tuple[int, int]


ADMISSION_LIMIT = database.ADMITTED_REQUEST_LIMIT
MUTATING_LIMIT = database.MUTATING_REQUEST_LIMIT
ADMISSION_CASES = (
    pytest.param(
        AdmissionCase("GET", 0, MUTATING_LIMIT, True, (ADMISSION_LIMIT - 1, NO_PERMITS)),
        id="get-admitted-while-every-mutating-permit-is-held",
    ),
    pytest.param(
        AdmissionCase("PUT", 0, 0, True, (ADMISSION_LIMIT - 1, MUTATING_LIMIT - 1)),
        id="put-takes-a-mutating-permit-and-an-admission",
    ),
    pytest.param(
        AdmissionCase("POST", 0, MUTATING_LIMIT, False, (ADMISSION_LIMIT, NO_PERMITS)),
        id="post-refused-without-a-mutating-permit",
    ),
    pytest.param(
        AdmissionCase("DELETE", ADMISSION_LIMIT, 0, False, (NO_PERMITS, MUTATING_LIMIT)),
        id="delete-refused-frees-its-mutating-permit",
    ),
    pytest.param(
        AdmissionCase("GET", ADMISSION_LIMIT, 0, False, (NO_PERMITS, MUTATING_LIMIT)),
        id="get-refused-without-an-admission",
    ),
)


def BuildDatabaseUrl(driverName: str, databasePassword: str) -> str:
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://admission_test:{databasePassword}@127.0.0.1/admission_test"


@pytest.fixture(name="admissionSettings")
def AdmissionSettings() -> Settings:
    """Return backend Settings with generated secrets and the short admission bound."""
    databasePassword = secrets.token_urlsafe(12)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(32),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
        DB_CONNECT_TIMEOUT_SECONDS=ADMISSION_WAIT_SECONDS,
    )


@pytest.fixture(name="connectAttempts")
def ConnectAttempts(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace asyncpg.connect, which every app-engine connection calls, with a recorder.

    The recorder raises ConnectionRefusedError, so a connection attempt the code under test
    should never make is both recorded and failed without reaching any network.
    """
    attempts: list[object] = []

    async def RecordConnect(*arguments: Any, **keywords: Any) -> asyncpg.Connection:
        attempts.append(keywords.get("host"))
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncpg, "connect", RecordConnect)
    return attempts


@pytest_asyncio.fixture(name="appState")
async def AppState(admissionSettings: Settings) -> AsyncIterator[SimpleNamespace]:
    """Yield the app.state stand-in main.CreateApp builds; dispose of its engines at the end."""
    engine = database.BuildEngine(admissionSettings)
    appState = SimpleNamespace(
        settings=admissionSettings,
        engine=engine,
        sessionFactory=database.BuildSessionFactory(engine),
    )
    try:
        yield appState
    finally:
        await database.DisposeEngine(engine)
        refusingEngine = getattr(appState, database.REFUSING_ENGINE_ATTRIBUTE, None)
        if refusingEngine is not None:
            await database.DisposeEngine(refusingEngine)


def RequestOf(appState: SimpleNamespace, method: str) -> SimpleNamespace:
    """Return a request stand-in of ``method`` to the app whose state is ``appState``."""
    return SimpleNamespace(
        app=SimpleNamespace(state=appState), method=method, state=SimpleNamespace()
    )


@contextlib.asynccontextmanager
async def HeldPermits(semaphore: Semaphore, permitCount: int) -> AsyncIterator[None]:
    """Hold ``permitCount`` permits of ``semaphore``, as that many admitted requests do."""
    async with contextlib.AsyncExitStack() as permitStack:
        for _ in range(permitCount):
            await permitStack.enter_async_context(semaphore)
        yield


@contextlib.asynccontextmanager
async def HeldByOthers(
    appState: SimpleNamespace, heldAdmissions: int, heldMutatingPermits: int
) -> AsyncIterator[None]:
    """Hold admissions and mutating permits of the app, as other admitted requests do."""
    otherRequest = RequestOf(appState, "GET")
    async with (
        HeldPermits(database.RequestAdmission(otherRequest), heldAdmissions),
        HeldPermits(database.MutatingRequestAdmission(otherRequest), heldMutatingPermits),
    ):
        yield


def FreePermits(request: SimpleNamespace) -> tuple[int, int]:
    """Return the free admissions and the free mutating permits of the request's app."""
    return (
        database.RequestAdmission(request).value,
        database.MutatingRequestAdmission(request).value,
    )


def IsRefused(request: SimpleNamespace, session: Any) -> bool:
    """Return whether the request and its session are bound to the app's refusing engine."""
    refusingEngine = getattr(request.app.state, database.REFUSING_ENGINE_ATTRIBUTE, None)
    return refusingEngine is not None and (
        session.bind is refusingEngine and locks.RequestEngine(request) is refusingEngine
    )


def SessionDependency(dependency: Callable[..., Any]) -> Callable[..., Any]:
    """Return a session dependency of app.core.database as an async context manager."""
    return contextlib.asynccontextmanager(dependency)


@pytest.mark.parametrize("case", ADMISSION_CASES)
async def test_get_session_admits_by_method_within_the_bound_or_refuses(
    case: AdmissionCase, appState: SimpleNamespace, connectAttempts: list[object]
) -> None:
    """Admitted or refused as the method and held permits decide; a refusal holds nothing."""
    request = RequestOf(appState, case.method)
    async with HeldByOthers(appState, case.heldAdmissions, case.heldMutatingPermits):
        startedAt = time.monotonic()
        async with SessionDependency(database.GetSession)(request) as session:
            elapsedSeconds = time.monotonic() - startedAt
            refused = IsRefused(request, session)
            assert (refused, FreePermits(request)) == (not case.admitted, case.freeInside)
    if case.admitted:
        assert elapsedSeconds < AT_ONCE_SECONDS
        assert locks.RequestEngine(request) is appState.engine
    else:
        assert elapsedSeconds >= ADMISSION_WAIT_SECONDS - CLOCK_TOLERANCE_SECONDS
        assert elapsedSeconds < ADMISSION_WAIT_SECONDS + REFUSAL_MARGIN_SECONDS
    assert (FreePermits(request), connectAttempts) == ((ADMISSION_LIMIT, MUTATING_LIMIT), [])


@pytest.mark.parametrize(
    "failureClasses",
    [TimeoutError, database.CONNECTION_FAILURES, locks.WINDOW_FAILURES],
    ids=["timeout-error", "connection-failures", "window-failures"],
)
def test_admission_timeout_is_an_ioerr_failure_of_every_layer(
    failureClasses: type[BaseException] | tuple[type[BaseException], ...],
) -> None:
    """The repositories, WriteUnit, the window and the throttle all catch the refusal."""
    assert issubclass(database.AdmissionTimeoutError, failureClasses)


async def test_refused_request_fails_every_connection_at_once_without_connecting(
    appState: SimpleNamespace, connectAttempts: list[object]
) -> None:
    """Session, window, write unit and check slot of a refused request all fail as IOERR."""
    request = RequestOf(appState, "GET")
    async with (
        HeldByOthers(appState, ADMISSION_LIMIT, NO_PERMITS),
        SessionDependency(database.GetSession)(request) as session,
    ):
        context = OnlineContext(request=request, session=session, settings=appState.settings)
        startedAt = time.monotonic()
        with pytest.raises(database.AdmissionTimeoutError):
            await session.execute(PROBE_STATEMENT)
        with pytest.raises(FileIoError):
            await locks.EnterOnlineWindow(request, TRANSACT_FILE_NAME)
        with pytest.raises(FileIoError):
            async with database.WriteUnit(context.engine, ACCOUNT_FILE_NAME):
                pass
        with pytest.raises(FileIoError):
            await login_throttle.OpenSlotConnection(context.engine)
        assert time.monotonic() - startedAt < AT_ONCE_SECONDS
    assert (connectAttempts, appState.engine.pool.checkedout()) == ([], 0)


async def test_unadmitted_session_waits_for_no_admission_and_never_connects(
    appState: SimpleNamespace, connectAttempts: list[object]
) -> None:
    """The menus' session comes at once while every admission is held, and cannot connect."""
    request = RequestOf(appState, "POST")
    async with HeldByOthers(appState, ADMISSION_LIMIT, MUTATING_LIMIT):
        startedAt = time.monotonic()
        async with SessionDependency(database.GetUnadmittedSession)(request) as session:
            assert time.monotonic() - startedAt < AT_ONCE_SECONDS
            assert (IsRefused(request, session), FreePermits(request)) == (True, (0, 0))
            with pytest.raises(database.AdmissionTimeoutError):
                await session.execute(PROBE_STATEMENT)
    assert (connectAttempts, appState.engine.pool.checkedout()) == ([], 0)
