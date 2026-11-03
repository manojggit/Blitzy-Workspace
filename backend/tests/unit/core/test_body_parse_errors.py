"""Unit tests of the legacy error contract for JSON bodies FastAPI cannot parse.

Every error body carries the four legacy keys message, info_message, field_errors and
cursor_field, and no catch-all handler is registered, as specified in the Technical
Specification, Sections 0.6.4 and 0.6.5. FastAPI answers an application/json body that is not
valid UTF-8, or one nested too deeply to decode, with its own 400 ``{"detail"}`` body. These
tests pin that app.core.errors.HandleHttpException answers such a body on every JSON write
route with the sanitised 422 body of malformed JSON, sent with the session cookie of the role
the route's guard passes, while every other HTTP error keeps FastAPI's default answer and an
unexpected error stays FastAPI's 500.

A missing, expired or invalid cookie gives 401, and a role refusal 403 (Section 0.7.1), even
when the body cannot be parsed: app.api.v1.SessionFirstRoute runs the route's session guard
before FastAPI reads the body. Every guarded JSON write route of the app must be one.

The session guard and the body read both run before any database use, so no database is
needed: the app is built with CreateApp from Settings whose engine never connects. SECRET_KEY
and the user IDs are generated with the secrets module, and backend/.env is never read
(_env_file=None).
"""

import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import SessionFirstRoute
from app.core.config import Settings
from app.core.database import DisposeEngine
from app.core.dependencies import GetCurrentUser, RequireAdmin, RequireRegular
from app.core.messages import ADMIN_ONLY_MESSAGE
from app.core.security import SESSION_SIGNING_ALGORITHM, IssueSessionToken

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
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
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
# The legacy body of a session refusal, of an admin refused a regular route and of OriginGuard.
EMPTY_MESSAGE_RESPONSE = {"message": "", "info_message": "", "field_errors": [], "cursor_field": ""}
ADMIN_ONLY_RESPONSE = {**EMPTY_MESSAGE_RESPONSE, "message": ADMIN_ONLY_MESSAGE}

ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# Every type but exactly A is regular, lower-case a included (Section 0.7.1).
REGULAR_USER_TYPES = ["U", "u", "a", "X"]
USER_ID_HEX_BYTES = 4
# An expired token's exp is this long before now, and its iat this long before its exp.
EXPIRED_TOKEN_AGE = timedelta(minutes=5)

# The JSON write routes RequireAdmin guards (CA00, CU00-CU03) and those RequireRegular guards
# (CM00, CAUP, CCLI, CCUP, CT00, CT02, CB00). Path keys are neutral values no seed record has.
ADMIN_BODY_ROUTES = [
    ("POST", "/api/v1/menu/admin/selection"),
    ("POST", "/api/v1/admin/users/selection"),
    ("POST", "/api/v1/admin/users"),
    ("PUT", "/api/v1/admin/users/TESTU001"),
]
REGULAR_BODY_ROUTES = [
    ("POST", "/api/v1/menu/main/selection"),
    ("POST", "/api/v1/transactions/selection"),
    ("POST", "/api/v1/transactions"),
    ("POST", "/api/v1/billpay"),
    ("POST", "/api/v1/cards/selection"),
    ("PUT", "/api/v1/cards/9999000011112222"),
    ("PUT", "/api/v1/accounts/99999999999"),
]
GUARDED_BODY_ROUTES = ADMIN_BODY_ROUTES + REGULAR_BODY_ROUTES
# A lone 0xFF byte and a 0xFF inside a JSON string are not UTF-8 (UnicodeDecodeError); the
# nested arrays exceed the decoder's recursion limit (RecursionError).
UNPARSABLE_BODIES = [b"\xff", b'{"user_id":"\xff"}', b"[" * 100_000]
UNPARSABLE_BODY_IDS = ["lone-0xff-byte", "0xff-in-json-string", "nested-too-deep"]
# Bodies the session guard must answer before FastAPI tries to parse them.
MALFORMED_JSON_BODY = b"{bad"
SESSION_FIRST_BODIES = [MALFORMED_JSON_BODY, b"\xff", b"[" * 100_000]
SESSION_FIRST_BODY_IDS = ["json-decode-error", "lone-0xff-byte", "nested-too-deep"]

# Returns the session token a test request carries, minted for the app's Settings; None for
# no session cookie.
type TokenBuilder = Callable[[Settings], str | None]


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


def NewUserId() -> str:
    """Return a generated 8-character upper-case hex user ID, which no seed user ID is."""
    return secrets.token_hex(USER_ID_HEX_BYTES).upper()


def NoSessionToken(settings: Settings) -> str | None:
    """Return no token: the request carries no session cookie."""
    return None


def RoleTokenBuilder(userType: str) -> TokenBuilder:
    """Return a TokenBuilder of a valid session token for a generated user of ``userType``."""

    def BuildRoleToken(settings: Settings) -> str:
        """Return a token signed with the app's SECRET_KEY for a new user of ``userType``."""
        return IssueSessionToken(NewUserId(), userType, settings)

    return BuildRoleToken


def ForeignKeySessionToken(settings: Settings) -> str:
    """Return a regular user's token signed with another generated SECRET_KEY."""
    return IssueSessionToken(NewUserId(), REGULAR_USER_TYPE, BuildTestSettings())


def ExpiredSessionToken(settings: Settings) -> str:
    """Return a regular user's token signed with the app's SECRET_KEY and already expired."""
    expiresAt = datetime.now(UTC) - EXPIRED_TOKEN_AGE
    claims = {
        "sub": NewUserId(),
        "typ": REGULAR_USER_TYPE,
        "iat": expiresAt - EXPIRED_TOKEN_AGE,
        "exp": expiresAt,
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=SESSION_SIGNING_ALGORITHM)


def RouteCases(routes: list[tuple[str, str]], tokenBuilder: TokenBuilder) -> list:
    """Return one ``pytest.param(method, path, tokenBuilder)`` per route, named by the route."""
    return [
        pytest.param(method, path, tokenBuilder, id=f"{method}-{path}") for method, path in routes
    ]


ADMIN_TOKEN = RoleTokenBuilder(ADMIN_USER_TYPE)
REGULAR_TOKEN = RoleTokenBuilder(REGULAR_USER_TYPE)
# Every JSON write route of /api/v1, with a token of the role its guard passes; the public
# sign-on carries none.
JSON_WRITE_ROUTES = [
    *RouteCases([("POST", "/api/v1/auth/login")], NoSessionToken),
    *RouteCases(ADMIN_BODY_ROUTES, ADMIN_TOKEN),
    *RouteCases(REGULAR_BODY_ROUTES, REGULAR_TOKEN),
]
# A missing, invalid or expired cookie: GetCurrentUser's 401 (Section 0.7.1).
REFUSED_TOKENS = [NoSessionToken, ForeignKeySessionToken, ExpiredSessionToken]
REFUSED_TOKEN_IDS = ["no-cookie", "foreign-key-token", "expired-token"]
# An admin on a regular route gets an empty message; every regular type on an admin route
# gets the admin-only literal.
WRONG_ROLE_CASES = [
    *[
        pytest.param((method, path), ADMIN_TOKEN, EMPTY_MESSAGE_RESPONSE, id=f"A-{method}-{path}")
        for method, path in REGULAR_BODY_ROUTES
    ],
    *[
        pytest.param(
            (method, path),
            RoleTokenBuilder(userType),
            ADMIN_ONLY_RESPONSE,
            id=f"{userType}-{method}-{path}",
        )
        for userType in REGULAR_USER_TYPES
        for method, path in ADMIN_BODY_ROUTES
    ],
]
SESSION_GUARD_CALLS = (GetCurrentUser, RequireAdmin, RequireRegular)


def DependsOnSessionGuard(dependant: Dependant) -> bool:
    """Return True when ``dependant`` or any dependency beneath it is a session guard."""
    if dependant.call in SESSION_GUARD_CALLS:
        return True
    return any(DependsOnSessionGuard(subDependant) for subDependant in dependant.dependencies)


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


@pytest_asyncio.fixture(name="sessionClient")
async def SessionClient(
    request: pytest.FixtureRequest, bodyParseApp: FastAPI
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a bodyParseApp client whose requests carry the token the TokenBuilder param builds.

    The token travels as the SESSION_COOKIE_NAME cookie; a None token sends no cookie.
    """
    settings = bodyParseApp.state.settings
    token = request.param(settings)
    async with BuildClient(bodyParseApp) as client:
        if token is not None:
            client.headers["Cookie"] = f"{settings.SESSION_COOKIE_NAME}={token}"
        yield client


@pytest.mark.parametrize("requestBody", UNPARSABLE_BODIES, ids=UNPARSABLE_BODY_IDS)
@pytest.mark.parametrize(
    ("method", "path", "sessionClient"), JSON_WRITE_ROUTES, indirect=["sessionClient"]
)
async def test_unparsable_json_body_gets_legacy_validation_body(
    method, path, sessionClient, requestBody
):
    response = await sessionClient.request(method, path, content=requestBody, headers=JSON_HEADERS)
    assert response.status_code == HTTP_UNPROCESSABLE
    assert response.json() == BODY_PARSE_RESPONSE
    assert FASTAPI_PARSE_DETAIL_TEXT not in response.text


@pytest.mark.parametrize("requestBody", SESSION_FIRST_BODIES, ids=SESSION_FIRST_BODY_IDS)
@pytest.mark.parametrize("sessionClient", REFUSED_TOKENS, ids=REFUSED_TOKEN_IDS, indirect=True)
@pytest.mark.parametrize(("method", "path"), GUARDED_BODY_ROUTES)
async def test_refused_session_gets_401_before_the_body_is_read(
    method, path, sessionClient, requestBody
):
    response = await sessionClient.request(method, path, content=requestBody, headers=JSON_HEADERS)
    assert response.status_code == HTTP_UNAUTHORIZED
    assert response.json() == EMPTY_MESSAGE_RESPONSE


@pytest.mark.parametrize("requestBody", SESSION_FIRST_BODIES, ids=SESSION_FIRST_BODY_IDS)
@pytest.mark.parametrize(
    ("route", "sessionClient", "expectedResponse"), WRONG_ROLE_CASES, indirect=["sessionClient"]
)
async def test_wrong_role_gets_403_before_the_body_is_read(
    route, sessionClient, expectedResponse, requestBody
):
    method, path = route
    response = await sessionClient.request(method, path, content=requestBody, headers=JSON_HEADERS)
    assert response.status_code == HTTP_FORBIDDEN
    assert response.json() == expectedResponse


@pytest.mark.parametrize(("method", "path"), GUARDED_BODY_ROUTES)
async def test_origin_guard_refuses_before_the_session_guard(method, path, bodyParseApp):
    transport = httpx.ASGITransport(app=bodyParseApp)
    async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
        response = await client.request(
            method, path, content=MALFORMED_JSON_BODY, headers=JSON_HEADERS
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert response.json() == EMPTY_MESSAGE_RESPONSE


def test_every_guarded_body_route_checks_the_session_first(bodyParseApp):
    guardedBodyRoutes = [
        route
        for route in bodyParseApp.routes
        if isinstance(route, APIRoute)
        and route.body_field is not None
        and DependsOnSessionGuard(route.dependant)
    ]
    assert len(guardedBodyRoutes) == len(GUARDED_BODY_ROUTES)
    notSessionFirstPaths = [
        route.path for route in guardedBodyRoutes if not isinstance(route, SessionFirstRoute)
    ]
    assert notSessionFirstPaths == []


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
