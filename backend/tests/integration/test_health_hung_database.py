"""Integration tests of GET /health/ready while the database stops responding (QA finding F01).

Covers the readiness bound of the Technical Specification, Section 0.6.5: the probe runs
SELECT 1 within HEALTH_READY_TIMEOUT_SECONDS and otherwise answers 503 unavailable, its
cleanup included, even when the database keeps the connection open and never answers, as it
does under ``docker pause``. StallableProxy (tests/database_proxy.py) relays the test database
through a local port and can hold every byte while keeping both sockets open, which is what a
paused database looks like to the client. The database is stalled after a warm probe, at the
probe's first query, and before any connection.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import Engine

from app.core.config import Settings
from tests.database_proxy import ProxiedDatabaseUrl, ProxyOf, StallableProxy

API_BASE_URL = "http://127.0.0.1:8000"
READINESS_PATH = "/health/ready"
HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503
READY_BODY = {"status": "ready"}
UNAVAILABLE_BODY = {"status": "unavailable"}

PROBE_TIMEOUT_SECONDS = 1.0
# How long after PROBE_TIMEOUT_SECONDS the unavailable answer may still arrive.
RESPONSE_MARGIN_SECONDS = 1.5
# A request still running after this long fails the test instead of hanging it.
REQUEST_LIMIT_SECONDS = 10.0
# Time for the proxy to see the probe's terminated sockets close.
CLEANUP_GRACE_SECONDS = 0.1

STALL_AFTER_WARM_PROBE = "after-warm-probe"
STALL_AT_FIRST_QUERY = "at-first-query"
STALL_BEFORE_CONNECT = "before-connect"


async def RequestReadiness(client: httpx.AsyncClient) -> httpx.Response:
    """GET /health/ready, raising TimeoutError after REQUEST_LIMIT_SECONDS instead of hanging."""
    async with asyncio.timeout(REQUEST_LIMIT_SECONDS):
        return await client.get(READINESS_PATH)


async def AssertReady(client: httpx.AsyncClient) -> None:
    """Assert that GET /health/ready answers 200 with the ready body."""
    response = await RequestReadiness(client)
    assert (response.status_code, response.json()) == (HTTP_OK, READY_BODY)


async def StallDatabase(
    stallPoint: str, stallableProxy: StallableProxy, client: httpx.AsyncClient
) -> None:
    """Make the proxied database stop answering at stallPoint, one of the STALL_* values."""
    if stallPoint == STALL_AFTER_WARM_PROBE:
        await AssertReady(client)
        stallableProxy.Stall()
    elif stallPoint == STALL_AT_FIRST_QUERY:
        stallableProxy.StallAtFirstQuery()
    else:
        stallableProxy.Stall()


@pytest_asyncio.fixture(name="stallableProxy")
async def StallableProxyFixture(
    testSettings: Settings, syncEngine: Engine
) -> AsyncIterator[StallableProxy]:
    """Yield a started StallableProxy of the test database; close it at teardown.

    syncEngine refuses a database whose name lacks _test before the proxy relays to it.
    """
    stallableProxy = ProxyOf(testSettings.DATABASE_URL)
    await stallableProxy.Start()
    try:
        yield stallableProxy
    finally:
        await stallableProxy.Close()


@pytest_asyncio.fixture(name="proxiedClient")
async def ProxiedClientFixture(
    testSettings: Settings, stallableProxy: StallableProxy
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of CreateApp on the proxied database, timing readiness out after 1 second.

    The lifespan is entered here, as httpx.ASGITransport sends no lifespan events. Teardown
    resumes the proxy first, so a failed test's held connections can close, then leaves the
    lifespan and disposes of the engine. app.main is imported here, not at module level, because
    importing it builds the module-level app from the process settings.
    """
    from app.core.database import DisposeEngine
    from app.main import CreateApp

    proxiedSettings = testSettings.model_copy(
        update={
            "DATABASE_URL": ProxiedDatabaseUrl(testSettings.DATABASE_URL, stallableProxy.port),
            "HEALTH_READY_TIMEOUT_SECONDS": PROBE_TIMEOUT_SECONDS,
        }
    )
    application = CreateApp(proxiedSettings)
    async with contextlib.AsyncExitStack() as exitStack:
        exitStack.push_async_callback(DisposeEngine, application.state.engine)
        await exitStack.enter_async_context(application.router.lifespan_context(application))
        exitStack.callback(stallableProxy.Resume)
        transport = httpx.ASGITransport(app=application)
        client = httpx.AsyncClient(transport=transport, base_url=API_BASE_URL)
        yield await exitStack.enter_async_context(client)


@pytest.mark.parametrize(
    "stallPoint", (STALL_AFTER_WARM_PROBE, STALL_AT_FIRST_QUERY, STALL_BEFORE_CONNECT)
)
async def test_readiness_is_unavailable_within_timeout_when_database_stops_responding(
    stallPoint: str, stallableProxy: StallableProxy, proxiedClient: httpx.AsyncClient
) -> None:
    """A stalled database gives 503 unavailable within the timeout, then ready once it answers.

    No probe connection is left open on the proxy afterwards, so the probe's cleanup did not
    wait on the stalled database.
    """
    await StallDatabase(stallPoint, stallableProxy, proxiedClient)
    startedAt = time.monotonic()
    stalledResponse = await RequestReadiness(proxiedClient)
    elapsedSeconds = time.monotonic() - startedAt
    assert stalledResponse.status_code == HTTP_SERVICE_UNAVAILABLE
    assert stalledResponse.json() == UNAVAILABLE_BODY
    assert elapsedSeconds < PROBE_TIMEOUT_SECONDS + RESPONSE_MARGIN_SECONDS
    await asyncio.sleep(CLEANUP_GRACE_SECONDS)
    assert stallableProxy.openClientCount == 0
    stallableProxy.Resume()
    await AssertReady(proxiedClient)
