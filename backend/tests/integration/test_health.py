"""Integration tests of the liveness and readiness probes, GET /health and GET /health/ready.

Covers the health and readiness probes of the Technical Specification, Section 0.6.5. They
replace no legacy program: the CICS region of csd/CARDDEMO.CSD made the CARDDEMO files
available implicitly, and the probes report that availability for the HTTP service. /health
answers ok without a session cookie, an Origin header or the database. /health/ready answers
ready while the test database runs SELECT 1, and 503 unavailable within its bounded timeouts
when the database refuses connections. Both stay outside the /api/v1 prefix (Section 0.6.9).
The cases of a database that keeps its connection open and stops answering are in
test_health_hung_database.py.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from app.core.config import API_V1_PREFIX, Settings

API_BASE_URL = "http://127.0.0.1:8000"
HEALTH_URL = "/health"
READY_URL = "/health/ready"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_SERVICE_UNAVAILABLE = 503
OK_BODY = {"status": "ok"}
READY_BODY = {"status": "ready"}
UNAVAILABLE_BODY = {"status": "unavailable"}

# Nothing listens on TCP port 1 of the loopback host, so every connect is refused at once.
UNREACHABLE_HOST = "127.0.0.1"
UNREACHABLE_PORT = 1
UNREACHABLE_TIMEOUT_SECONDS = 1.0
# A probe answer later than this fails the test instead of hanging it; with the 1-second
# connect and readiness timeouts it also proves the unavailable answer is bounded.
RESPONSE_LIMIT_SECONDS = 3.0


def BuildUnreachableSettings(testSettings: Settings) -> Settings:
    """Return testSettings pointed at a refusing port, with 1-second connect and probe timeouts.

    The URL keeps the credentials and database of testSettings.DATABASE_URL; only its host and
    port change, so no credential text is written here.
    """
    unreachableUrl = make_url(testSettings.DATABASE_URL).set(
        host=UNREACHABLE_HOST, port=UNREACHABLE_PORT
    )
    return testSettings.model_copy(
        update={
            "DATABASE_URL": unreachableUrl.render_as_string(hide_password=False),
            "DB_CONNECT_TIMEOUT_SECONDS": UNREACHABLE_TIMEOUT_SECONDS,
            "HEALTH_READY_TIMEOUT_SECONDS": UNREACHABLE_TIMEOUT_SECONDS,
        }
    )


def BuildAnonymousClient(application: FastAPI) -> httpx.AsyncClient:
    """Return a client of application that sends no session cookie and no Origin header."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=API_BASE_URL)


async def RequestWithinLimit(client: httpx.AsyncClient, path: str) -> httpx.Response:
    """GET path, raising TimeoutError when no answer arrives within RESPONSE_LIMIT_SECONDS."""
    async with asyncio.timeout(RESPONSE_LIMIT_SECONDS):
        return await client.get(path)


@pytest_asyncio.fixture(name="unreachableClient")
async def UnreachableClientFixture(testSettings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an anonymous client of CreateApp on the unreachable database, inside its lifespan.

    httpx.ASGITransport sends no lifespan events, so the lifespan is entered here, as the testApp
    fixture does; leaving it disposes of the app's engine. app.main is imported here, not at
    module level, because importing it builds the module-level app from the process settings.
    """
    from app.main import CreateApp

    application = CreateApp(BuildUnreachableSettings(testSettings))
    async with (
        application.router.lifespan_context(application),
        BuildAnonymousClient(application) as client,
    ):
        yield client


async def test_health_returns_ok_without_session(testApp: FastAPI) -> None:
    """GET /health answers 200 ok to a client that sends no session cookie and no Origin."""
    async with BuildAnonymousClient(testApp) as anonymousClient:
        healthResponse = await RequestWithinLimit(anonymousClient, HEALTH_URL)
    assert "cookie" not in healthResponse.request.headers
    assert "origin" not in healthResponse.request.headers
    assert healthResponse.status_code == HTTP_OK
    assert healthResponse.json() == OK_BODY


async def test_ready_returns_ready_when_database_reachable(apiClient: httpx.AsyncClient) -> None:
    """GET /health/ready answers 200 ready while the test database runs SELECT 1."""
    readyResponse = await RequestWithinLimit(apiClient, READY_URL)
    assert readyResponse.status_code == HTTP_OK
    assert readyResponse.json() == READY_BODY


async def test_ready_returns_503_when_database_unreachable(
    unreachableClient: httpx.AsyncClient,
) -> None:
    """A refused database connection gives 503 unavailable in time; /health still answers ok.

    The probe runs unpatched, so the refused connect reaches it as OSError, one of the
    failures it turns into 503 rather than 500.
    """
    readyResponse = await RequestWithinLimit(unreachableClient, READY_URL)
    healthResponse = await RequestWithinLimit(unreachableClient, HEALTH_URL)
    assert readyResponse.status_code == HTTP_SERVICE_UNAVAILABLE
    assert readyResponse.json() == UNAVAILABLE_BODY
    assert healthResponse.status_code == HTTP_OK
    assert healthResponse.json() == OK_BODY


@pytest.mark.parametrize("probePath", (HEALTH_URL, READY_URL))
async def test_probes_are_not_under_api_prefix(
    probePath: str, apiClient: httpx.AsyncClient
) -> None:
    """The probes are served at the root only: under /api/v1 their paths answer 404."""
    prefixedResponse = await RequestWithinLimit(apiClient, API_V1_PREFIX + probePath)
    assert prefixedResponse.status_code == HTTP_NOT_FOUND
