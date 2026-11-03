"""Unit tests for app.core.security, the session token that GetCurrentUser verifies.

The unit replaces RACF sign-on and the plaintext SEC-USR-PWD compare (cpy/CSUSR01Y.cpy,
cbl/COSGN00C.cbl), the COMMAREA identity CDEMO-USER-ID and CDEMO-USER-TYPE (cpy/COCOM01Y.cpy),
and the COMMAREA card key and screen state, as specified in the Technical Specification,
Section 0.7.1. These tests pin that a session cookie whose header is nested too deeply to parse
is refused as an invalid token, so the request gets the legacy 401 with an empty message
instead of a 500. Every key and password is generated at run time with the secrets module, and
backend/.env is never read (_env_file=None).
"""

import base64
import json
import secrets

import jwt
import pytest
from starlette.requests import Request

from app.core.config import Settings
from app.core.dependencies import GetCurrentUser
from app.core.errors import STANDARD_BODY_KEYS, LegacyMessageError
from app.core.security import ReadSessionToken

# Far past the JSON decoder's nesting limit (about 10,000 levels on CPython 3.13.7), so
# parsing the header raises RecursionError rather than ValueError.
NESTED_HEADER_DEPTH = 20000
SESSION_REFUSAL_STATUS = 401


def BuildDatabaseUrl(driverName, databasePassword):
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://security_test:{databasePassword}@127.0.0.1/security_test"


def EncodeSegment(segmentBytes):
    """Return segmentBytes as an unpadded base64url JWT segment."""
    return base64.urlsafe_b64encode(segmentBytes).rstrip(b"=").decode("ascii")


def BuildNestedHeaderText(depth):
    """Return the JSON object {"x": ...} whose value is 0 inside depth nested arrays."""
    return '{"x":' + "[" * depth + "0" + "]" * depth + "}"


def BuildCookieRequest(cookieName, cookieValue):
    """Return a Starlette GET request whose only header is Cookie: cookieName=cookieValue."""
    cookieHeader = f"{cookieName}={cookieValue}".encode("latin-1")
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookieHeader)]}
    )


@pytest.fixture(name="sessionSettings")
def SessionSettings():
    """Return backend Settings holding a generated SECRET_KEY and database password."""
    databasePassword = secrets.token_urlsafe(12)
    return Settings(
        _env_file=None,
        SECRET_KEY=secrets.token_urlsafe(32),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


@pytest.fixture(name="nestedHeaderToken")
def NestedHeaderToken():
    """Return a token with the deeply nested header, an empty-object payload and no signature."""
    headerSegment = EncodeSegment(BuildNestedHeaderText(NESTED_HEADER_DEPTH).encode("ascii"))
    return f"{headerSegment}.{EncodeSegment(b'{}')}."


def test_nested_header_exceeds_json_nesting_limit():
    with pytest.raises(RecursionError):
        json.loads(BuildNestedHeaderText(NESTED_HEADER_DEPTH))


def test_read_session_token_refuses_nested_header(nestedHeaderToken, sessionSettings):
    with pytest.raises(jwt.InvalidTokenError):
        ReadSessionToken(nestedHeaderToken, sessionSettings)


async def test_get_current_user_refuses_nested_header_with_empty_401(
    nestedHeaderToken, sessionSettings
):
    request = BuildCookieRequest(sessionSettings.SESSION_COOKIE_NAME, nestedHeaderToken)
    assert request.cookies[sessionSettings.SESSION_COOKIE_NAME] == nestedHeaderToken
    with pytest.raises(LegacyMessageError) as refusal:
        await GetCurrentUser(request, sessionSettings)
    refusalBody = refusal.value.Body()
    assert refusal.value.httpStatus == SESSION_REFUSAL_STATUS
    assert tuple(refusalBody) == STANDARD_BODY_KEYS
    assert refusalBody["message"] == ""
