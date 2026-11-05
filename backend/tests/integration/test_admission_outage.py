"""Integration tests of request admission while the database accepts connections, never answering.

Replaces: the WHEN OTHER file-error branches the online programs take when a file cannot be
reached: COTRN00C's TRANSACT browse (``Unable to lookup transaction...``), COCRDLIC's CARDDAT
browse (the WS-FILE-ERROR-MESSAGE READ literal), CORPT00C's TDQ write (``Unable to Write TDQ
(JOBS)...``) and COSGN00C's USRSEC read (``Unable to verify the User ...``); and COMEN01C, which
does no file I/O. Covers the Technical Specification, Sections 0.6.5 (database access bounded by
DB_CONNECT_TIMEOUT_SECONDS; WHEN OTHER literals answer 503) and 0.6.13 (bounded database
timeouts), for QA findings QAPF2-F01 and QAPF2-F02. With the database stalled (``docker
pause``) and every request admission held by CT00 requests stuck on it:

- a further CT00, CCLI, CR00 and sign-on are refused within DB_CONNECT_TIMEOUT_SECONDS plus a
  margin, each with its program's 503 literal, while the stuck requests still wait, and GET
  /auth/me answers 200 with blank display names; the refused sign-on counts no failure;
- CM00's GET /menu/main and POST /menu/main/selection answer 200 at once;
- no refused request checks out a pooled connection: the pool's checked-out count never rises
  above what the stuck requests hold;
- once the database answers again, every request is served and every admission is free.

The app reaches the test database only through StallableProxy (tests/database_proxy.py), so no
test pauses the shared PostgreSQL server. Clients, users and the database are fixtures of
tests/conftest.py; the refused sign-on's user ID and password are generated with the secrets
module, and restoreSeedState undoes every write.
"""

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import Pool

from app.core.config import Settings
from app.core.database import (
    ADMITTED_REQUEST_LIMIT,
    MUTATING_REQUEST_LIMIT,
    MutatingRequestAdmission,
    RequestAdmission,
)
from tests.database_proxy import ProxiedDatabaseUrl, ProxyOf, StallableProxy

# pytest reads a module's marks only under this exact lower-case name. Usefixtures are set up
# first and torn down last, so the app engine is disposed of before the seed restore runs.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

# --- Driver timeouts of the proxied app, and the bounds asserted -------------------------------
# The admission bound: a refused request answers this long after it arrives.
CONNECT_TIMEOUT_SECONDS = 1.0
# Long against the admission bound, so the stuck requests, waiting out a pre-ping on a warm
# connection, hold their admissions until well after every refusal.
COMMAND_TIMEOUT_SECONDS = 8.0
# How long after its bound a refusal may still arrive.
RESPONSE_MARGIN_SECONDS = 1.5
# A request still running after this long fails the test instead of hanging it.
REQUEST_LIMIT_SECONDS = 30.0
WAIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.02

# --- HTTP ----------------------------------------------------------------------------------------
TRANSACTIONS_URL = "/api/v1/transactions"
CARDS_URL = "/api/v1/cards"
REPORT_URL = "/api/v1/reports/transactions"
LOGIN_URL = "/api/v1/auth/login"
AUTH_ME_URL = "/api/v1/auth/me"
MAIN_MENU_URL = "/api/v1/menu/main"
MAIN_SELECTION_URL = MAIN_MENU_URL + "/selection"
HTTP_OK = 200
HTTP_FILE_ERROR = 503
MONTHLY_CONFIRMED: Mapping[str, str] = MappingProxyType({"reportType": "monthly", "confirm": "Y"})
# COMEN02Y option 1, Account View, an installed option.
ACCOUNT_VIEW_OPTION: Mapping[str, str] = MappingProxyType({"option": "1"})
# A generated sign-on user ID: one letter and six hex digits, within SEC-USR-ID X(08).
SIGN_ON_USER_PREFIX = "R"
SIGN_ON_USER_HEX_BYTES = 3
# token_urlsafe(6) gives 8 characters, within SEC-USR-PWD X(08).
SIGN_ON_CREDENTIAL_BYTES = 6

# --- The refused requests and the menus, with what each answers ----------------------------------
CT00 = "CT00"
CCLI = "CCLI"
CR00 = "CR00"
CC00 = "CC00"
AUTH_ME = "auth-me"
MAIN_MENU = "CM00 menu"
MAIN_SELECTION = "CM00 selection"
# Literals verbatim from the COBOL sources.
REFUSAL_LITERALS: Mapping[str, str] = MappingProxyType(
    {
        CT00: "Unable to lookup transaction...",
        CCLI: "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000",
        CR00: "Unable to Write TDQ (JOBS)...",
        CC00: "Unable to verify the User ...",
    }
)
REFUSED_NAMES = (*REFUSAL_LITERALS, AUTH_ME)
MENU_NAMES = (MAIN_MENU, MAIN_SELECTION)
BLANK_NAMES = ("", "")

PROBE_STATEMENT = text("SELECT 1")
LOGIN_ATTEMPTS_STATEMENT = text(
    "SELECT count(*) FROM security.login_attempts WHERE user_id = :userId"
)
WAIT_FAILED_MESSAGE = "The stuck requests did not hold every admission within {seconds} s."


@dataclass(frozen=True)
class TimedResponse:
    """A response and the seconds it took to arrive."""

    response: httpx.Response
    elapsedSeconds: float


# --- Fixtures ------------------------------------------------------------------------------------
@pytest_asyncio.fixture(name="stallableProxy")
async def StallableProxyFixture(
    databaseUrls: Any, syncEngine: Engine
) -> AsyncIterator[StallableProxy]:
    """Yield a started StallableProxy of the test database; close it at teardown.

    syncEngine refuses a database whose name lacks _test before the proxy relays to it.
    """
    stallableProxy = ProxyOf(databaseUrls.asyncUrl)
    await stallableProxy.Start()
    try:
        yield stallableProxy
    finally:
        await stallableProxy.Close()


@pytest.fixture(name="testSettings")
def ProxiedSettingsFixture(testSettings: Settings, stallableProxy: StallableProxy) -> Settings:
    """Return the conftest settings with the database behind the proxy and these timeouts.

    testApp, and so every client of tests/conftest.py, is built from these settings.
    """
    return testSettings.model_copy(
        update={
            "DATABASE_URL": ProxiedDatabaseUrl(testSettings.DATABASE_URL, stallableProxy.port),
            "DB_CONNECT_TIMEOUT_SECONDS": CONNECT_TIMEOUT_SECONDS,
            "DB_COMMAND_TIMEOUT_SECONDS": COMMAND_TIMEOUT_SECONDS,
        }
    )


@pytest_asyncio.fixture(name="outageProxy")
async def OutageProxyFixture(
    stallableProxy: StallableProxy, testApp: FastAPI
) -> AsyncIterator[StallableProxy]:
    """Yield the proxy of testApp's database; resume it before testApp disposes of its engine."""
    try:
        yield stallableProxy
    finally:
        stallableProxy.Resume()


# --- Requests, admissions and the pool -----------------------------------------------------------
async def WithinLimit(request: Awaitable[httpx.Response]) -> httpx.Response:
    """Await a request, raising TimeoutError after REQUEST_LIMIT_SECONDS instead of hanging."""
    async with asyncio.timeout(REQUEST_LIMIT_SECONDS):
        return await request


async def Timed(request: Awaitable[httpx.Response]) -> TimedResponse:
    """Await a request within REQUEST_LIMIT_SECONDS and return it with its elapsed seconds."""
    startedAt = time.monotonic()
    response = await WithinLimit(request)
    return TimedResponse(response, time.monotonic() - startedAt)


def FreeAdmissions(application: FastAPI) -> tuple[int, int]:
    """Return the app's free request admissions and free mutating permits."""
    appRequest = SimpleNamespace(app=application)
    return (
        RequestAdmission(appRequest).value,
        MutatingRequestAdmission(appRequest).value,
    )


async def WarmPool(engine: AsyncEngine, connectionCount: int) -> None:
    """Open ``connectionCount`` pooled connections at once and return them, so they stay pooled."""
    async with contextlib.AsyncExitStack() as connectionStack:
        for _ in range(connectionCount):
            connection = await connectionStack.enter_async_context(engine.connect())
            await connection.execute(PROBE_STATEMENT)


async def WaitUntil(condition: Callable[[], bool]) -> None:
    """Poll ``condition`` until it holds; fail the test after WAIT_TIMEOUT_SECONDS."""
    eventLoop = asyncio.get_running_loop()
    deadline = eventLoop.time() + WAIT_TIMEOUT_SECONDS
    while not condition():
        if eventLoop.time() >= deadline:
            pytest.fail(WAIT_FAILED_MESSAGE.format(seconds=WAIT_TIMEOUT_SECONDS))
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def StuckRequests(
    client: httpx.AsyncClient, application: FastAPI, stallableProxy: StallableProxy
) -> AsyncIterator[list[asyncio.Task[httpx.Response]]]:
    """Stall the database, then admit ADMITTED_REQUEST_LIMIT CT00 requests that wait on it.

    The pool is warm, so each stuck request's first checkout pre-pings a pooled connection and
    waits out the long command timeout, holding its admission. The tasks are yielded once every
    admission is held and each stuck request has its connection checked out; any still running
    at exit, as after a failed assertion, are cancelled and awaited.
    """
    pool: Pool = application.state.engine.pool
    await WarmPool(application.state.engine, ADMITTED_REQUEST_LIMIT)
    stallableProxy.Stall()
    stuckRequests = [
        asyncio.create_task(WithinLimit(client.get(TRANSACTIONS_URL)))
        for _ in range(ADMITTED_REQUEST_LIMIT)
    ]
    try:
        await WaitUntil(
            lambda: (
                FreeAdmissions(application) == (0, MUTATING_REQUEST_LIMIT)
                and pool.checkedout() >= ADMITTED_REQUEST_LIMIT
            )
        )
        yield stuckRequests
    finally:
        for stuckRequest in stuckRequests:
            stuckRequest.cancel()
        await asyncio.gather(*stuckRequests, return_exceptions=True)


def SignOnBody() -> dict[str, str]:
    """Return a sign-on body of a generated user ID and password that no user has."""
    userId = SIGN_ON_USER_PREFIX + secrets.token_hex(SIGN_ON_USER_HEX_BYTES).upper()
    return {"user_id": userId, "password": secrets.token_urlsafe(SIGN_ON_CREDENTIAL_BYTES).upper()}


def RequestsDuringRefusal(
    client: httpx.AsyncClient, signOnBody: Mapping[str, str]
) -> dict[str, Awaitable[httpx.Response]]:
    """Return the requests that must be refused, and the menu requests, by name."""
    return {
        CT00: client.get(TRANSACTIONS_URL),
        CCLI: client.get(CARDS_URL),
        CR00: client.get(REPORT_URL, params=dict(MONTHLY_CONFIRMED)),
        CC00: client.post(LOGIN_URL, json=dict(signOnBody)),
        AUTH_ME: client.get(AUTH_ME_URL),
        MAIN_MENU: client.get(MAIN_MENU_URL),
        MAIN_SELECTION: client.post(MAIN_SELECTION_URL, json=dict(ACCOUNT_VIEW_OPTION)),
    }


async def SamplePool(pool: Pool, samples: list[int]) -> None:
    """Record the pool's checked-out count every POLL_INTERVAL_SECONDS until cancelled."""
    while True:
        samples.append(pool.checkedout())
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def SendWhileSampling(
    requests: Mapping[str, Awaitable[httpx.Response]], pool: Pool
) -> tuple[dict[str, TimedResponse], int]:
    """Send the requests at once; return their timed responses and the peak checked-out count."""
    samples = [pool.checkedout()]
    sampler = asyncio.create_task(SamplePool(pool, samples))
    try:
        timedResponses = await asyncio.gather(*(Timed(request) for request in requests.values()))
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    return dict(zip(requests, timedResponses, strict=True)), max(samples)


# --- Assertions ----------------------------------------------------------------------------------
def AssertRefused(timedResponses: Mapping[str, TimedResponse]) -> None:
    """Assert each refused request's WHEN OTHER answer, within the admission bound and margin."""
    for name, literal in REFUSAL_LITERALS.items():
        response = timedResponses[name].response
        assert (response.status_code, response.json()["message"]) == (HTTP_FILE_ERROR, literal)
    meBody = timedResponses[AUTH_ME].response.json()
    assert timedResponses[AUTH_ME].response.status_code == HTTP_OK, meBody
    assert (meBody["first_name"], meBody["last_name"]) == BLANK_NAMES
    for name in REFUSED_NAMES:
        elapsedSeconds = timedResponses[name].elapsedSeconds
        assert elapsedSeconds < CONNECT_TIMEOUT_SECONDS + RESPONSE_MARGIN_SECONDS, name


def AssertMenusAtOnce(timedResponses: Mapping[str, TimedResponse]) -> None:
    """Assert both menu requests answered 200 before any admission wait could have ended."""
    for name in MENU_NAMES:
        timedResponse = timedResponses[name]
        assert timedResponse.response.status_code == HTTP_OK, timedResponse.response.text
        assert timedResponse.elapsedSeconds < CONNECT_TIMEOUT_SECONDS, name


async def AssertRecovered(
    client: httpx.AsyncClient, application: FastAPI, stuckRequests: list[asyncio.Task[Any]]
) -> None:
    """Once the database answers: the stuck requests end, reads are served, admissions free."""
    stuckResponses = await asyncio.gather(*stuckRequests)
    assert {response.status_code for response in stuckResponses} <= {HTTP_OK, HTTP_FILE_ERROR}
    for url in (TRANSACTIONS_URL, CARDS_URL, AUTH_ME_URL, MAIN_MENU_URL):
        response = await WithinLimit(client.get(url))
        assert response.status_code == HTTP_OK, (url, response.text)
    assert FreeAdmissions(application) == (ADMITTED_REQUEST_LIMIT, MUTATING_REQUEST_LIMIT)


def CountLoginAttempts(syncEngine: Engine, userId: str) -> int:
    """Return the security.login_attempts rows of ``userId``, on a sync connection of the test."""
    with syncEngine.connect() as connection:
        return connection.execute(LOGIN_ATTEMPTS_STATEMENT, {"userId": userId}).scalar_one()


# --- Tests ---------------------------------------------------------------------------------------
async def test_requests_queued_behind_a_stalled_database_are_refused_and_menus_answer_at_once(
    outageProxy: StallableProxy,
    testApp: FastAPI,
    regularClient: httpx.AsyncClient,
    syncEngine: Engine,
) -> None:
    """Refused within the admission bound with their literals; menus at once; nothing leaks."""
    pool: Pool = testApp.state.engine.pool
    signOnBody = SignOnBody()
    async with StuckRequests(regularClient, testApp, outageProxy) as stuckRequests:
        stuckCheckedOut = pool.checkedout()
        requests = RequestsDuringRefusal(regularClient, signOnBody)
        timedResponses, peakCheckedOut = await SendWhileSampling(requests, pool)
        assert not any(stuckRequest.done() for stuckRequest in stuckRequests)
        AssertRefused(timedResponses)
        AssertMenusAtOnce(timedResponses)
        assert peakCheckedOut <= stuckCheckedOut, (peakCheckedOut, stuckCheckedOut)
        outageProxy.Resume()
        await AssertRecovered(regularClient, testApp, stuckRequests)
    assert CountLoginAttempts(syncEngine, signOnBody["user_id"]) == 0
