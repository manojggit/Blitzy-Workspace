"""Unit tests of the CSRF origin guard app.core.middleware.OriginGuard.

Replaces: the cross-site write protection the CICS region gave CardDemo implicitly
(csd/CARDDEMO.CSD), where only a 3270 terminal session could start a transaction and sign-on
COSGN00C (CC00) was the one entry before any session existed. OriginGuard supplies that
protection for the session cookie, as the Technical Specification, Section 0.7.1, defines it:
a POST, PUT or DELETE whose Origin header is missing or not allow-listed is answered 403 with
the legacy error body and an empty message before any handler runs, and POST
/api/v1/auth/login, the COSGN00C port, is the only exempt write. Section 0.11.1 makes
http://127.0.0.1:3000 the one local frontend origin, so http://localhost:3000, a different
site, is refused.

The guard wraps a bare FastAPI app whose routes only echo, so no settings, secret or database
are needed.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.config import API_V1_PREFIX
from app.core.middleware import GUARDED_METHODS, ORIGIN_EXEMPT_ROUTES, OriginGuard

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
ACCOUNT_PATH = API_V1_PREFIX + "/accounts/x"
LOGIN_PATH = API_V1_PREFIX + "/auth/login"
# No Origin at all, a foreign site, and localhost, a different site from 127.0.0.1.
REFUSED_ORIGINS = (None, "http://evil.example", "http://localhost:3000")
REFUSED_ORIGIN_IDS = ["missing", "foreign-site", "localhost"]
GUARDED = ("POST", "PUT", "DELETE")
ACCOUNT_METHODS = ["GET", *GUARDED]
HTTP_OK = 200
HTTP_FORBIDDEN = 403
HANDLER_BODY = {"ok": True}
EMPTY_LEGACY_BODY = {"message": "", "info_message": "", "field_errors": [], "cursor_field": ""}


async def EchoOk() -> dict[str, bool]:
    """Answer every test route, so a response carrying HANDLER_BODY shows the handler ran."""
    return dict(HANDLER_BODY)


def BuildGuardedApp() -> OriginGuard:
    """Return an echoing FastAPI app wrapped in OriginGuard allow-listing only ALLOWED_ORIGIN."""
    application = FastAPI()
    application.add_api_route(ACCOUNT_PATH, EchoOk, methods=ACCOUNT_METHODS)
    application.add_api_route(LOGIN_PATH, EchoOk, methods=["POST"])
    return OriginGuard(application, allowedOrigins=[ALLOWED_ORIGIN])


@pytest_asyncio.fixture(name="guardedClient")
async def GuardedClient() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of BuildGuardedApp that sends no Origin unless a request adds one."""
    transport = httpx.ASGITransport(app=BuildGuardedApp())
    async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
        yield client


def OriginHeaders(origin: str | None) -> dict[str, str]:
    """Return headers carrying ``origin`` as the Origin header, or no header when it is None."""
    if origin is None:
        return {}
    return {"Origin": origin}


@pytest.mark.parametrize("origin", REFUSED_ORIGINS, ids=REFUSED_ORIGIN_IDS)
@pytest.mark.parametrize("method", GUARDED)
async def test_guarded_write_without_allowed_origin_is_refused(guardedClient, method, origin):
    response = await guardedClient.request(method, ACCOUNT_PATH, headers=OriginHeaders(origin))
    assert response.status_code == HTTP_FORBIDDEN
    assert response.json() == EMPTY_LEGACY_BODY
    assert response.json() != HANDLER_BODY


@pytest.mark.parametrize("method", GUARDED)
async def test_allowed_origin_passes(guardedClient, method):
    response = await guardedClient.request(
        method, ACCOUNT_PATH, headers=OriginHeaders(ALLOWED_ORIGIN)
    )
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY


async def test_get_without_origin_passes(guardedClient):
    response = await guardedClient.get(ACCOUNT_PATH)
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY


async def test_login_is_exempt_without_origin(guardedClient):
    response = await guardedClient.post(LOGIN_PATH)
    assert response.status_code == HTTP_OK
    assert response.json() == HANDLER_BODY


def test_only_login_is_exempt():
    assert ORIGIN_EXEMPT_ROUTES == frozenset({("POST", LOGIN_PATH)})
    assert set(GUARDED_METHODS) == {"POST", "PUT", "DELETE"}


def test_single_origin_string_is_rejected_as_misconfiguration():
    # A lone string would allow-list its characters and refuse every write.
    with pytest.raises(TypeError):
        OriginGuard(FastAPI(), allowedOrigins=ALLOWED_ORIGIN)
