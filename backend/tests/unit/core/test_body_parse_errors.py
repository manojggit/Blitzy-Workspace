"""Unit tests of the legacy error contract for JSON bodies FastAPI cannot parse.

Every error body carries the four legacy keys message, info_message, field_errors and
cursor_field, and no catch-all handler is registered, as specified in the Technical
Specification, Sections 0.6.4 and 0.6.5. FastAPI answers an application/json body that is not
valid UTF-8, or one nested too deeply to decode, with its own 400 ``{"detail"}`` body. These
tests pin that app.core.errors.HandleHttpException answers such a body on every JSON write
route with the sanitised 422 body of malformed JSON, while every other HTTP error keeps
FastAPI's default answer and an unexpected error stays FastAPI's 500.

The body is read before any dependency runs, so no database is needed: the app is built with
CreateApp from Settings whose engine never connects. SECRET_KEY is generated with the secrets
module and backend/.env is never read (_env_file=None).
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import Settings
from app.core.database import DisposeEngine

ALLOWED_ORIGIN = "http://127.0.0.1:3000"
API_BASE_URL = "http://127.0.0.1:8000"
# A well-formed URL of a database nobody creates: the engine is lazy and never connects.
UNUSED_DATABASE_URL = "postgresql+{driver}://body_parse_test@127.0.0.1/body_parse_test"
SECRET_KEY_BYTES = 32
TEST_APP_ENV = "test"
JSON_HEADERS = {"Content-Type": "application/json"}

# FastAPI's own text for the body-parse 400; the 422 answer must not carry it.
FASTAPI_PARSE_DETAIL_TEXT = "There was an error parsing the body"
CAUSELESS_PARSE_400_PATH = "/test-only/causeless-parse-400"
UNEXPECTED_ERROR_PATH = "/test-only/unexpected-error"
HTTP_BAD_REQUEST = 400
HTTP_UNPROCESSABLE = 422
HTTP_SERVER_ERROR = 500
INTERNAL_SERVER_ERROR_TEXT = "Internal Server Error"

BODY_PARSE_RESPONSE = {
    "detail": [{"loc": ["body"], "msg": "JSON decode error", "type": "json_invalid"}],
    "message": "",
    "info_message": "",
    "field_errors": [],
    "cursor_field": "",
}
# Every JSON write route of /api/v1. Path keys are neutral values no seed record has.
JSON_WRITE_ROUTES = [
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/menu/main/selection"),
    ("POST", "/api/v1/menu/admin/selection"),
    ("POST", "/api/v1/admin/users/selection"),
    ("POST", "/api/v1/admin/users"),
    ("PUT", "/api/v1/admin/users/TESTU001"),
    ("POST", "/api/v1/transactions/selection"),
    ("POST", "/api/v1/transactions"),
    ("POST", "/api/v1/billpay"),
    ("POST", "/api/v1/cards/selection"),
    ("PUT", "/api/v1/cards/9999000011112222"),
    ("PUT", "/api/v1/accounts/99999999999"),
]
# A lone 0xFF byte and a 0xFF inside a JSON string are not UTF-8 (UnicodeDecodeError); the
# nested arrays exceed the decoder's recursion limit (RecursionError).
UNPARSABLE_BODIES = [b"\xff", b'{"user_id":"\xff"}', b"[" * 100_000]
UNPARSABLE_BODY_IDS = ["lone-0xff-byte", "0xff-in-json-string", "nested-too-deep"]


@dataclass(frozen=True)
class DefaultErrorCase:
    """An HTTP error FastAPI's default handler answers, and the answer it must keep."""

    method: str
    path: str
    status: int
    detail: str
    allow: str | None = None


DEFAULT_ERROR_CASES = [
    DefaultErrorCase("GET", "/api/v1/does-not-exist", 404, "Not Found"),
    DefaultErrorCase("GET", "/api/v1/billpay", 405, "Method Not Allowed", allow="POST"),
    DefaultErrorCase("GET", CAUSELESS_PARSE_400_PATH, HTTP_BAD_REQUEST, FASTAPI_PARSE_DETAIL_TEXT),
]
DEFAULT_ERROR_IDS = ["unknown-path-404", "unsupported-method-405", "causeless-parse-detail-400"]


async def RaiseCauselessParseDetail() -> None:
    """Raise a 400 with FastAPI's body-parse detail but no cause, as application code might."""
    raise StarletteHTTPException(status_code=HTTP_BAD_REQUEST, detail=FASTAPI_PARSE_DETAIL_TEXT)


async def RaiseUnexpectedError() -> None:
    """Raise an error no handler is registered for."""
    raise RuntimeError("unexpected failure")


def BuildTestSettings() -> Settings:
    """Return Settings with a generated key, the allow-listed origin and an unused database."""
    return Settings(
        _env_file=None,
        APP_ENV=TEST_APP_ENV,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=UNUSED_DATABASE_URL.format(driver="asyncpg"),
        SYNC_DATABASE_URL=UNUSED_DATABASE_URL.format(driver="psycopg2"),
        BACKEND_CORS_ORIGINS=ALLOWED_ORIGIN,
    )


def BuildBodyParseApp() -> FastAPI:
    """Return CreateApp's app with two test-only routes: a causeless 400 and an unexpected error.

    app.main is imported here, not at module level, as in the shared testApp fixture.
    """
    from app.main import CreateApp

    application = CreateApp(BuildTestSettings())
    application.add_api_route(CAUSELESS_PARSE_400_PATH, RaiseCauselessParseDetail)
    application.add_api_route(UNEXPECTED_ERROR_PATH, RaiseUnexpectedError)
    return application


def BuildClient(application: FastAPI, raiseAppExceptions: bool = True) -> httpx.AsyncClient:
    """Return a client of ``application`` that sends the allow-listed Origin OriginGuard checks.

    ``raiseAppExceptions=False`` returns the 500 response of an unhandled error instead of
    re-raising the error in the test.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=raiseAppExceptions),
        base_url=API_BASE_URL,
        headers={"Origin": ALLOWED_ORIGIN},
    )


@pytest_asyncio.fixture(name="bodyParseApp", scope="module")
async def BodyParseApp() -> AsyncIterator[FastAPI]:
    """Yield the app of BuildBodyParseApp; dispose of its never-connected engine after."""
    application = BuildBodyParseApp()
    yield application
    await DisposeEngine(application.state.engine)


@pytest_asyncio.fixture(name="bodyParseClient")
async def BodyParseClient(bodyParseApp: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of bodyParseApp, closed at teardown."""
    async with BuildClient(bodyParseApp) as client:
        yield client


@pytest.mark.parametrize("requestBody", UNPARSABLE_BODIES, ids=UNPARSABLE_BODY_IDS)
@pytest.mark.parametrize(("method", "path"), JSON_WRITE_ROUTES)
async def test_unparsable_json_body_gets_legacy_validation_body(
    method, path, requestBody, bodyParseClient
):
    response = await bodyParseClient.request(
        method, path, content=requestBody, headers=JSON_HEADERS
    )
    assert response.status_code == HTTP_UNPROCESSABLE
    assert response.json() == BODY_PARSE_RESPONSE
    assert FASTAPI_PARSE_DETAIL_TEXT not in response.text


@pytest.mark.parametrize("errorCase", DEFAULT_ERROR_CASES, ids=DEFAULT_ERROR_IDS)
async def test_other_http_errors_keep_fastapi_default_answer(errorCase, bodyParseClient):
    response = await bodyParseClient.request(errorCase.method, errorCase.path)
    assert response.status_code == errorCase.status
    assert response.json() == {"detail": errorCase.detail}
    assert response.headers.get("allow") == errorCase.allow


async def test_unexpected_error_stays_fastapi_500(bodyParseApp):
    async with BuildClient(bodyParseApp, raiseAppExceptions=False) as client:
        response = await client.get(UNEXPECTED_ERROR_PATH)
    assert response.status_code == HTTP_SERVER_ERROR
    assert response.text == INTERNAL_SERVER_ERROR_TEXT


def test_no_catch_all_handler_is_registered(bodyParseApp):
    assert Exception not in bodyParseApp.exception_handlers
    assert HTTP_SERVER_ERROR not in bodyParseApp.exception_handlers
