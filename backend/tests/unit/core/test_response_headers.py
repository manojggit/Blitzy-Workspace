"""Tests that every response of the CreateApp stack carries the response security headers.

app.core.middleware.ResponseSecurityHeaders is the outermost layer CreateApp installs, outside
CORS, Starlette's error middleware and OriginGuard. So the four headers of
RESPONSE_SECURITY_HEADERS reach the health probe, the API docs, a session refusal, the guard's
403, FastAPI's 404 and 422, an unexpected 500 and both CORS preflight answers, and the CORS
headers of each answer stay what CORS alone sends: Access-Control-Allow-Origin only for the
allow-listed origin, and Access-Control-Allow-Credentials wherever CORS answered the request.

None of these responses reads the database, so the app is built with CreateApp from Settings
whose engine never connects. SECRET_KEY is generated with the secrets module, and backend/.env
is never read (_env_file=None).
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.config import Settings
from app.core.database import DisposeEngine
from app.core.middleware import RESPONSE_SECURITY_HEADERS

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
# Another host name of the same frontend, which BACKEND_CORS_ORIGINS does not list.
UNLISTED_ORIGIN = "http://localhost:3000"
API_BASE_URL = "http://127.0.0.1:8000"
# A well-formed URL of a database nobody creates: the engine is lazy and never connects.
UNUSED_DATABASE_URL = "postgresql+{driver}://response_headers_test@127.0.0.1/response_headers_test"
SECRET_KEY_BYTES = 32
TEST_APP_ENV = "test"
UNEXPECTED_ERROR_PATH = "/test-only/unexpected-error"
ALLOW_ORIGIN_HEADER = "access-control-allow-origin"
ALLOW_CREDENTIALS_HEADER = "access-control-allow-credentials"
ALLOW_CREDENTIALS_VALUE = "true"
ALLOWED_ORIGIN_HEADERS = {"Origin": ALLOWED_ORIGIN}
PREFLIGHT_PUT_HEADERS = {
    "Access-Control-Request-Method": "PUT",
    "Access-Control-Request-Headers": "content-type",
}
MALFORMED_JSON_HEADERS = {"Content-Type": "application/json"}
MALFORMED_JSON_BODY = b"{bad"
ACCOUNT_PATH = "/api/v1/accounts/99999999999"


@dataclass(frozen=True)
class SecuredAnswerCase:
    """A request, its status, and the CORS headers its answer must carry beside the policy."""

    method: str
    path: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None
    allowOrigin: str | None = None
    allowCredentials: str | None = None


SECURED_ANSWER_CASES = [
    pytest.param(SecuredAnswerCase("GET", "/health", 200), id="health-200"),
    pytest.param(SecuredAnswerCase("GET", "/docs", 200), id="docs-200"),
    pytest.param(SecuredAnswerCase("GET", "/openapi.json", 200), id="openapi-200"),
    pytest.param(SecuredAnswerCase("GET", "/api/v1/auth/me", 401), id="no-session-401"),
    pytest.param(SecuredAnswerCase("PUT", ACCOUNT_PATH, 403), id="origin-guard-403"),
    pytest.param(SecuredAnswerCase("GET", "/api/v1/does-not-exist", 404), id="unknown-path-404"),
    pytest.param(
        SecuredAnswerCase(
            "POST",
            "/api/v1/auth/login",
            422,
            headers=MALFORMED_JSON_HEADERS,
            content=MALFORMED_JSON_BODY,
        ),
        id="malformed-json-422",
    ),
    pytest.param(
        SecuredAnswerCase(
            "GET",
            UNEXPECTED_ERROR_PATH,
            500,
            headers=ALLOWED_ORIGIN_HEADERS,
            allowOrigin=ALLOWED_ORIGIN,
            allowCredentials=ALLOW_CREDENTIALS_VALUE,
        ),
        id="unexpected-error-500",
    ),
    pytest.param(
        SecuredAnswerCase(
            "OPTIONS",
            ACCOUNT_PATH,
            200,
            headers={**ALLOWED_ORIGIN_HEADERS, **PREFLIGHT_PUT_HEADERS},
            allowOrigin=ALLOWED_ORIGIN,
            allowCredentials=ALLOW_CREDENTIALS_VALUE,
        ),
        id="allowed-preflight-200",
    ),
    pytest.param(
        SecuredAnswerCase(
            "OPTIONS",
            ACCOUNT_PATH,
            400,
            headers={"Origin": UNLISTED_ORIGIN, **PREFLIGHT_PUT_HEADERS},
            allowCredentials=ALLOW_CREDENTIALS_VALUE,
        ),
        id="unlisted-origin-preflight-400",
    ),
]


async def RaiseUnexpectedError() -> None:
    raise RuntimeError("unexpected failure")


def BuildTestSettings() -> Settings:
    return Settings(
        _env_file=None,
        APP_ENV=TEST_APP_ENV,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=UNUSED_DATABASE_URL.format(driver="asyncpg"),
        SYNC_DATABASE_URL=UNUSED_DATABASE_URL.format(driver="psycopg2"),
        BACKEND_CORS_ORIGINS=ALLOWED_ORIGIN,
    )


@pytest_asyncio.fixture(name="securedClient", scope="module")
async def SecuredClient() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of CreateApp's app plus a test-only failing route; send no Origin.

    raise_app_exceptions=False returns the 500 of the unhandled error instead of re-raising
    it in the test. app.main is imported here, not at module level, as in the shared testApp
    fixture.
    """
    from app.main import CreateApp

    application: FastAPI = CreateApp(BuildTestSettings())
    application.add_api_route(UNEXPECTED_ERROR_PATH, RaiseUnexpectedError)
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
        yield client
    await DisposeEngine(application.state.engine)


@pytest.mark.parametrize("answerCase", SECURED_ANSWER_CASES)
async def test_every_answer_carries_the_security_headers_and_unchanged_cors(
    securedClient, answerCase
):
    response = await securedClient.request(
        answerCase.method, answerCase.path, headers=answerCase.headers, content=answerCase.content
    )
    assert response.status_code == answerCase.status
    for headerName, headerValue in RESPONSE_SECURITY_HEADERS.items():
        assert response.headers.get_list(headerName) == [headerValue], headerName
    assert response.headers.get(ALLOW_ORIGIN_HEADER) == answerCase.allowOrigin
    assert response.headers.get(ALLOW_CREDENTIALS_HEADER) == answerCase.allowCredentials
