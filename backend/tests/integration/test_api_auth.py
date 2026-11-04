"""Integration tests of CC00 / COSGN00C sign-on, the session cookie and effective roles.

Replaces: COSGN00C (CC00), BMS COSGN00, copybooks CSUSR01Y, CSMSG01Y and COCOM01Y.

Every case goes through the ASGI app under /api/v1 against the migrated test database, proving
the Technical Specification's Sections 0.6.9 and 0.7.1 and success item 4 of Section 0.9.1:

- the COSGN00C literals in first-failure order (cbl/COSGN00C.cbl lines 117-254), the user ID and
  password upper-cased before the USRSEC read and compare (lines 132-136), and the routing of
  type exactly A to /admin and of every other type to /menu (lines 230-239);
- the HttpOnly session cookie: its attributes, no token in any body, GET /auth/me, and 401 with
  an empty message for a missing, tampered, expired or foreign-signed cookie;
- sign-off (PF3) with the CSMSG01Y thank-you literal, and the CSRF origin guard, from which only
  POST /auth/login is exempt;
- the login throttle of security.login_attempts: the lockout, the reset on success and the
  expiry of the lock;
- effective roles: only type A reaches the admin endpoints, every other type the regular ones.

The WHEN OTHER literal ``Unable to verify the User ...`` belongs to the batch-window cases of
test_concurrency.py. Clients, generated users, the USRSEC seed users and the seed restore are
fixtures of tests/conftest.py, so no password or signing key is written here.
"""

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx
import jwt
import pytest
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.messages import LOGIN_LOCKED_MESSAGE
from app.services.auth import login_throttle

pytestmark = pytest.mark.usefixtures("restoreSeedState")

LOGIN_URL = "/api/v1/auth/login"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"
# CA00 (COADM01C) and CU00 (COUSR00C), guarded by RequireAdmin; CM00 (COMEN01C) and CCLI
# (COCRDLIC), guarded by RequireRegular.
ADMIN_ENDPOINTS = ("/api/v1/menu/admin", "/api/v1/admin/users")
REGULAR_ENDPOINTS = ("/api/v1/menu/main", "/api/v1/cards")

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429

# COSGN00C literals (lines 120, 125, 242, 249) and CSMSG01Y CCDA-MSG-THANK-YOU, verbatim.
USER_ID_REQUIRED_LITERAL = "Please enter User ID ..."
CREDENTIAL_REQUIRED_LITERAL = "Please enter Password ..."
USER_NOT_FOUND_LITERAL = "User not found. Try again ..."
WRONG_CREDENTIAL_LITERAL = "Wrong Password. Try again ..."
THANK_YOU_LITERAL = "Thank you for using CardDemo application..."
# The COMEN01C admin-only literal, trailing space included, and the one literal the throttle adds.
ADMIN_ONLY_LITERAL = "No access - Admin Only option... "
LOCKED_LITERAL = "Too many failed attempts. Try again later ..."
NO_MESSAGE = ""
# MOVE -1 TO USERIDL / PASSWDL of COSGN0AI, named as the LoginRequest fields.
USER_ID_CURSOR = "user_id"
CREDENTIAL_CURSOR = "password"

ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# COSGN00C sends only CDEMO-USRTYP-ADMIN (exactly A) to COADM01C; every other stored type, lower-
# case a and u included, goes to COMEN01C.
ROLE_ROUTES = {"A": "/admin", "U": "/menu", "u": "/menu", "a": "/menu", "X": "/menu"}
ROLE_TYPES = tuple(ROLE_ROUTES)
SEED_ROLE_TYPES = (ADMIN_USER_TYPE, REGULAR_USER_TYPE)

FOREIGN_ORIGIN = "http://evil.example"
ORIGIN_HEADER = "Origin"
SET_COOKIE_HEADER = "set-cookie"
# A body key holding this fragment would carry the session token, which only the cookie may.
FORBIDDEN_BODY_KEY_FRAGMENT = "token"
# A blank USERID or PASSWD map field: empty, or its 8 positions of spaces.
BLANK_FIELDS = (pytest.param("", id="empty"), pytest.param(" " * 8, id="spaces"))
# Logout refused by OriginGuard: an Origin outside BACKEND_CORS_ORIGINS, or none at all.
REFUSED_ORIGINS = (pytest.param(FOREIGN_ORIGIN, id="foreign"), pytest.param(None, id="missing"))

# Session tokens built outside the app, with the claims core.security issues.
SESSION_ALGORITHM = "HS256"
BUILT_TOKEN_LIFETIME_SECONDS = 300
EXPIRED_TOKEN_AGE_SECONDS = 7200
EXPIRED_TOKEN_LIFETIME_SECONDS = 1
FOREIGN_KEY_BYTES = 48
SECONDS_PER_MINUTE = 60
# 8 characters, the COSGN00 PASSWD and SEC-USR-ID lengths.
GENERATED_CREDENTIAL_BYTES = 6
UNKNOWN_USER_ID_BYTES = 4

# Read-only SQL with bound values.
USER_EXISTS_STATEMENT = text("SELECT 1 FROM security.users WHERE user_id = :userId")
USER_NAMES_STATEMENT = text(
    "SELECT first_name, last_name FROM security.users WHERE user_id = :userId"
)
FAILURE_COUNT_STATEMENT = text(
    "SELECT failure_count FROM security.login_attempts WHERE user_id = :userId"
)


class GeneratedUser(Protocol):
    """The sign-on user createUser commits: its ID, its password as created and its type."""

    userId: str
    password: str
    userType: str


class SignedOnClient(Protocol):
    """What loginAs returns: a client holding the session cookie of ``user``."""

    client: httpx.AsyncClient
    user: GeneratedUser


class SeedUser(Protocol):
    """One USRSEC image record of the seedUsers fixture, with its image password."""

    userId: str
    firstName: str
    lastName: str
    password: str
    userType: str


type CreateUser = Callable[[str], GeneratedUser]
type LoginAs = Callable[[str], Awaitable[SignedOnClient]]
type InvalidTokenBuilder = Callable[[Settings, SignedOnClient], str]


@dataclass(frozen=True)
class SetCookie:
    """One Set-Cookie header: the cookie name and value, and its lower-cased attributes.

    A flag attribute such as HttpOnly maps to "".
    """

    name: str
    value: str
    attributes: dict[str, str]


# --- Response checks -----------------------------------------------------------------------------
def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> None:
    """Assert the status and the exact legacy error body (AAP 0.6.5).

    The body is {message, info_message, field_errors, cursor_field}: a sign-on failure names its
    cursor field in field_errors, and a session or origin refusal names none.
    """
    assert response.status_code == status, response.text
    assert response.json() == {
        "message": message,
        "info_message": "",
        "field_errors": [cursorField] if cursorField else [],
        "cursor_field": cursorField,
    }


def ParseSetCookie(response: httpx.Response, cookieName: str) -> SetCookie:
    """Return the response's one Set-Cookie header of ``cookieName``, parsed."""
    cookieHeaders = [
        header
        for header in response.headers.get_list(SET_COOKIE_HEADER)
        if header.startswith(cookieName + "=")
    ]
    assert len(cookieHeaders) == 1, f"{len(cookieHeaders)} Set-Cookie headers of {cookieName}"
    namePart, *attributeParts = cookieHeaders[0].split(";")
    name, _separator, value = namePart.partition("=")
    attributes = {}
    for attributePart in attributeParts:
        attributeName, _separator, attributeValue = attributePart.strip().partition("=")
        attributes[attributeName.lower()] = attributeValue
    return SetCookie(name=name, value=value, attributes=attributes)


def CookieIsExpired(setCookie: SetCookie) -> bool:
    """Return True when the Set-Cookie deletes its cookie: Max-Age=0 or an Expires in the past."""
    if setCookie.attributes.get("max-age") == "0":
        return True
    expires = setCookie.attributes.get("expires")
    return expires is not None and parsedate_to_datetime(expires) < datetime.now(UTC)


def ExpectedUserBody(syncEngine: Engine, userId: str, userType: str) -> dict[str, str]:
    """Return the LoginResponse and GET /auth/me body of a stored user of ``userType``.

    The names are read from security.users; the route is the ROLE_ROUTES entry of the type.
    """
    with syncEngine.connect() as connection:
        firstName, lastName = connection.execute(USER_NAMES_STATEMENT, {"userId": userId}).one()
    return {
        "user_id": userId,
        "user_type": userType,
        "first_name": firstName,
        "last_name": lastName,
        "next_route": ROLE_ROUTES[userType],
    }


def ReadFailureCount(syncEngine: Engine, userId: str) -> int | None:
    """Return the user ID's committed failure_count, or None when it has no throttle row."""
    with syncEngine.connect() as connection:
        result = connection.execute(FAILURE_COUNT_STATEMENT, {"userId": userId})
        return result.scalar_one_or_none()


# --- Requests ------------------------------------------------------------------------------------
async def SignOn(client: httpx.AsyncClient, userId: str, password: str) -> httpx.Response:
    """Send ENTER on COSGN00: the USERID and PASSWD fields to POST /api/v1/auth/login."""
    return await client.post(LOGIN_URL, json={"user_id": userId, "password": password})


async def SendWithoutOrigin(
    client: httpx.AsyncClient, method: str, url: str, body: dict[str, str] | None = None
) -> httpx.Response:
    """Send a request of ``client`` with its cookies but with the Origin header removed."""
    request = client.build_request(method, url, json=body)
    del request.headers[ORIGIN_HEADER]
    return await client.send(request)


async def PostLogout(client: httpx.AsyncClient, origin: str | None) -> httpx.Response:
    """Post PF3 (logout) with the given Origin header, or with none when ``origin`` is None."""
    if origin is None:
        return await SendWithoutOrigin(client, "POST", LOGOUT_URL)
    return await client.post(LOGOUT_URL, headers={ORIGIN_HEADER: origin})


async def FailSignOns(
    client: httpx.AsyncClient, userId: str, correctCredential: str, attempts: int
) -> None:
    """Sign ``userId`` on ``attempts`` times with wrong passwords; each answers the 401 literal."""
    for _attempt in range(attempts):
        response = await SignOn(client, userId, NewWrongCredential(correctCredential))
        AssertLegacyError(response, HTTP_UNAUTHORIZED, WRONG_CREDENTIAL_LITERAL, CREDENTIAL_CURSOR)


# --- Generated inputs ----------------------------------------------------------------------------
def NewCredential() -> str:
    """Return a generated 8-character password, printable in code page 037."""
    return secrets.token_urlsafe(GENERATED_CREDENTIAL_BYTES)


def NewWrongCredential(correctCredential: str) -> str:
    """Return a generated password that differs from ``correctCredential`` once upper-cased."""
    wrongCredential = NewCredential()
    while wrongCredential.upper() == correctCredential.upper():
        wrongCredential = NewCredential()
    return wrongCredential


def NewUnknownUserId(syncEngine: Engine) -> str:
    """Return an 8-character upper-case hex user ID holding a letter, absent from security.users.

    The letter makes its lower-case variant differ from it.
    """
    with syncEngine.connect() as connection:
        while True:
            candidateId = secrets.token_hex(UNKNOWN_USER_ID_BYTES).upper()
            exists = connection.execute(USER_EXISTS_STATEMENT, {"userId": candidateId}).first()
            if not candidateId.isdigit() and exists is None:
                return candidateId


# --- Session tokens built outside the app --------------------------------------------------------
def BuildSessionToken(signingKey: str, userId: str, issuedAt: int, lifetimeSeconds: int) -> str:
    """Return an HS256 token with the sub, typ (U), iat and exp claims core.security requires."""
    claims = {
        "sub": userId,
        "typ": REGULAR_USER_TYPE,
        "iat": issuedAt,
        "exp": issuedAt + lifetimeSeconds,
    }
    return jwt.encode(claims, signingKey, algorithm=SESSION_ALGORITHM)


def BuildCurrentSessionToken(signingKey: str, userId: str) -> str:
    """Return a token issued now and valid for BUILT_TOKEN_LIFETIME_SECONDS."""
    return BuildSessionToken(signingKey, userId, int(time.time()), BUILT_TOKEN_LIFETIME_SECONDS)


def BuildExpiredSessionToken(settings: Settings, userId: str) -> str:
    """Return a token signed with SECRET_KEY whose exp lies EXPIRED_TOKEN_AGE_SECONDS back."""
    pastIssuedAt = int(time.time()) - EXPIRED_TOKEN_AGE_SECONDS
    return BuildSessionToken(
        settings.SECRET_KEY, userId, pastIssuedAt, EXPIRED_TOKEN_LIFETIME_SECONDS
    )


def TamperSignature(sessionToken: str) -> str:
    """Return ``sessionToken`` with the first character of its signature segment changed.

    The first character carries six whole signature bits; the last carries padding bits too, so
    changing it alone could decode to the same signature.
    """
    header, payload, signature = sessionToken.split(".")
    changedCharacter = "B" if signature[0] == "A" else "A"
    return ".".join((header, payload, changedCharacter + signature[1:]))


def TamperedToken(settings: Settings, signedOn: SignedOnClient) -> str:
    """Return the signed-on client's real session cookie with one signature character changed."""
    return TamperSignature(signedOn.client.cookies[settings.SESSION_COOKIE_NAME])


def ExpiredToken(settings: Settings, signedOn: SignedOnClient) -> str:
    """Return an expired token of the signed-on user, signed with SECRET_KEY."""
    return BuildExpiredSessionToken(settings, signedOn.user.userId)


def ForeignKeyToken(settings: Settings, signedOn: SignedOnClient) -> str:
    """Return a current token of the signed-on user signed with a generated foreign key.

    ``settings`` is unused: every INVALID_COOKIE_BUILDERS entry takes the same two arguments.
    """
    foreignKey = secrets.token_urlsafe(FOREIGN_KEY_BYTES)
    return BuildCurrentSessionToken(foreignKey, signedOn.user.userId)


INVALID_COOKIE_BUILDERS = (
    pytest.param(TamperedToken, id="tampered"),
    pytest.param(ExpiredToken, id="expired"),
    pytest.param(ForeignKeyToken, id="foreign-key"),
)


# --- Sign-on literals, first-failure order (COSGN00C lines 117-254) ------------------------------
@pytest.mark.parametrize("blankUserId", BLANK_FIELDS)
async def test_blank_user_id_returns_user_id_literal_and_401(
    apiClient: httpx.AsyncClient, blankUserId: str
) -> None:
    """A blank USERID fails first, with the cursor on the user ID, whatever the password."""
    response = await SignOn(apiClient, blankUserId, NewCredential())
    AssertLegacyError(response, HTTP_UNAUTHORIZED, USER_ID_REQUIRED_LITERAL, USER_ID_CURSOR)
    assert SET_COOKIE_HEADER not in response.headers


@pytest.mark.parametrize("blankPassword", BLANK_FIELDS)
async def test_blank_password_returns_password_literal_and_401(
    apiClient: httpx.AsyncClient, createUser: CreateUser, blankPassword: str
) -> None:
    """A keyed user ID with a blank PASSWD fails before the read, cursor on the password."""
    user = createUser(REGULAR_USER_TYPE)
    response = await SignOn(apiClient, user.userId, blankPassword)
    AssertLegacyError(response, HTTP_UNAUTHORIZED, CREDENTIAL_REQUIRED_LITERAL, CREDENTIAL_CURSOR)
    assert SET_COOKIE_HEADER not in response.headers


async def test_unknown_user_id_returns_not_found_literal_and_401(
    apiClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """NOTFND on the USRSEC read (RESP 13) gives the not-found literal on the user ID."""
    response = await SignOn(apiClient, NewUnknownUserId(syncEngine), NewCredential())
    AssertLegacyError(response, HTTP_UNAUTHORIZED, USER_NOT_FOUND_LITERAL, USER_ID_CURSOR)
    assert SET_COOKIE_HEADER not in response.headers


async def test_wrong_password_returns_legacy_literal_and_401(
    apiClient: httpx.AsyncClient, createUser: CreateUser
) -> None:
    """A password that does not match the stored one gives the wrong-password literal."""
    user = createUser(REGULAR_USER_TYPE)
    response = await SignOn(apiClient, user.userId, NewWrongCredential(user.password))
    AssertLegacyError(response, HTTP_UNAUTHORIZED, WRONG_CREDENTIAL_LITERAL, CREDENTIAL_CURSOR)
    assert SET_COOKIE_HEADER not in response.headers


@pytest.mark.parametrize("userType", ROLE_TYPES)
async def test_sign_on_returns_the_user_and_the_route_of_its_type(
    apiClient: httpx.AsyncClient, createUser: CreateUser, syncEngine: Engine, userType: str
) -> None:
    """Success returns the stored user and type; only type exactly A is routed to /admin."""
    user = createUser(userType)
    response = await SignOn(apiClient, user.userId, user.password)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == ExpectedUserBody(syncEngine, user.userId, userType)


async def test_lower_case_user_id_and_password_still_sign_on(
    apiClient: httpx.AsyncClient, createUser: CreateUser, syncEngine: Engine
) -> None:
    """COSGN00C upper-cases USERID and PASSWD (lines 132-136) before the read and compare."""
    user = createUser(REGULAR_USER_TYPE)
    lowerUserId = user.userId.lower()
    assert lowerUserId != user.userId
    response = await SignOn(apiClient, lowerUserId, user.password.lower())
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == ExpectedUserBody(syncEngine, user.userId, REGULAR_USER_TYPE)


@pytest.mark.parametrize("userType", SEED_ROLE_TYPES)
async def test_seed_user_signs_on_with_its_image_password(
    apiClient: httpx.AsyncClient, seedUsers: tuple[SeedUser, ...], userType: str
) -> None:
    """The first seed admin and seed regular user of the USRSEC image sign on and are routed."""
    seedUser = next(seedUser for seedUser in seedUsers if seedUser.userType == userType)
    response = await SignOn(apiClient, seedUser.userId, seedUser.password)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {
        "user_id": seedUser.userId,
        "user_type": userType,
        "first_name": seedUser.firstName,
        "last_name": seedUser.lastName,
        "next_route": ROLE_ROUTES[userType],
    }


# --- Session cookie and GET /auth/me (AAP 0.7.1) -------------------------------------------------
async def test_sign_on_sets_http_only_lax_session_cookie(
    apiClient: httpx.AsyncClient, createUser: CreateUser, testSettings: Settings
) -> None:
    """The cookie is HttpOnly, SameSite=Lax, Path=/, lives the token lifetime, and is not Secure."""
    user = createUser(REGULAR_USER_TYPE)
    response = await SignOn(apiClient, user.userId, user.password)
    assert response.status_code == HTTP_OK, response.text
    setCookie = ParseSetCookie(response, testSettings.SESSION_COOKIE_NAME)
    tokenLifetime = testSettings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    assert setCookie.name == "carddemo_session"
    assert "httponly" in setCookie.attributes
    assert setCookie.attributes["path"] == "/"
    assert setCookie.attributes["samesite"].lower() == "lax"
    assert int(setCookie.attributes["max-age"]) == tokenLifetime
    assert testSettings.APP_ENV == "test"
    assert "secure" not in setCookie.attributes


async def test_sign_on_body_never_carries_the_session_token(
    apiClient: httpx.AsyncClient, createUser: CreateUser, testSettings: Settings
) -> None:
    """No body key names a token, and the cookie value appears nowhere in the response text."""
    user = createUser(REGULAR_USER_TYPE)
    response = await SignOn(apiClient, user.userId, user.password)
    assert response.status_code == HTTP_OK, response.text
    sessionToken = ParseSetCookie(response, testSettings.SESSION_COOKIE_NAME).value
    assert sessionToken
    assert not [key for key in response.json() if FORBIDDEN_BODY_KEY_FRAGMENT in key.lower()]
    assert sessionToken not in response.text


async def test_me_returns_the_signed_on_user_and_its_route(
    apiClient: httpx.AsyncClient, createUser: CreateUser, syncEngine: Engine
) -> None:
    """GET /auth/me with the cookie returns the user, its stored type and the login route."""
    user = createUser(REGULAR_USER_TYPE)
    signedOn = await SignOn(apiClient, user.userId, user.password)
    assert signedOn.status_code == HTTP_OK, signedOn.text
    response = await apiClient.get(ME_URL)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == signedOn.json()
    assert response.json() == ExpectedUserBody(syncEngine, user.userId, REGULAR_USER_TYPE)


async def test_me_without_cookie_returns_401_with_empty_message(
    apiClient: httpx.AsyncClient,
) -> None:
    """A request with no session cookie is refused with 401 and the empty legacy body."""
    response = await apiClient.get(ME_URL)
    AssertLegacyError(response, HTTP_UNAUTHORIZED, NO_MESSAGE)


async def test_me_accepts_a_valid_token_set_on_a_fresh_client(
    apiClient: httpx.AsyncClient, loginAs: LoginAs, testSettings: Settings
) -> None:
    """A token built here with SECRET_KEY and set on a fresh client is accepted.

    The refusal cases below build and send their cookies the same way, so each 401 they get is
    caused by the one fault they introduce.
    """
    signedOn = await loginAs(REGULAR_USER_TYPE)
    sessionToken = BuildCurrentSessionToken(testSettings.SECRET_KEY, signedOn.user.userId)
    apiClient.cookies.set(testSettings.SESSION_COOKIE_NAME, sessionToken)
    response = await apiClient.get(ME_URL)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["user_id"] == signedOn.user.userId


@pytest.mark.parametrize("buildInvalidToken", INVALID_COOKIE_BUILDERS)
async def test_me_refuses_an_invalid_session_cookie_with_401(
    apiClient: httpx.AsyncClient,
    loginAs: LoginAs,
    testSettings: Settings,
    buildInvalidToken: InvalidTokenBuilder,
) -> None:
    """A tampered, expired or foreign-signed cookie on a fresh client gives 401, empty message."""
    signedOn = await loginAs(REGULAR_USER_TYPE)
    invalidToken = buildInvalidToken(testSettings, signedOn)
    apiClient.cookies.set(testSettings.SESSION_COOKIE_NAME, invalidToken)
    response = await apiClient.get(ME_URL)
    AssertLegacyError(response, HTTP_UNAUTHORIZED, NO_MESSAGE)


# --- Logout and the CSRF origin guard ------------------------------------------------------------
async def test_logout_returns_thank_you_and_expires_the_cookie(
    loginAs: LoginAs, testSettings: Settings
) -> None:
    """PF3 returns the CSMSG01Y literal and deletes the cookie, so /auth/me then gives 401."""
    signedOn = await loginAs(REGULAR_USER_TYPE)
    response = await signedOn.client.post(LOGOUT_URL)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {"message": THANK_YOU_LITERAL}
    clearedCookie = ParseSetCookie(response, testSettings.SESSION_COOKIE_NAME)
    assert CookieIsExpired(clearedCookie)
    assert clearedCookie.attributes["path"] == "/"
    assert testSettings.SESSION_COOKIE_NAME not in signedOn.client.cookies
    AssertLegacyError(await signedOn.client.get(ME_URL), HTTP_UNAUTHORIZED, NO_MESSAGE)


@pytest.mark.parametrize("origin", REFUSED_ORIGINS)
async def test_logout_without_allowed_origin_is_refused_and_keeps_the_session(
    loginAs: LoginAs, origin: str | None
) -> None:
    """OriginGuard answers 403 with the empty body and no Set-Cookie; the session survives."""
    signedOn = await loginAs(REGULAR_USER_TYPE)
    response = await PostLogout(signedOn.client, origin)
    AssertLegacyError(response, HTTP_FORBIDDEN, NO_MESSAGE)
    assert SET_COOKIE_HEADER not in response.headers
    stillSignedOn = await signedOn.client.get(ME_URL)
    assert stillSignedOn.status_code == HTTP_OK, stillSignedOn.text


async def test_login_without_origin_is_exempt_from_the_guard(
    apiClient: httpx.AsyncClient, createUser: CreateUser, testSettings: Settings
) -> None:
    """POST /auth/login is the one guarded write exempt from the Origin check."""
    user = createUser(REGULAR_USER_TYPE)
    credentials = {"user_id": user.userId, "password": user.password}
    response = await SendWithoutOrigin(apiClient, "POST", LOGIN_URL, credentials)
    assert response.status_code == HTTP_OK, response.text
    assert ParseSetCookie(response, testSettings.SESSION_COOKIE_NAME).value


# --- Login throttle (AAP 0.7.1; security.login_attempts) -----------------------------------------
async def test_max_wrong_passwords_lock_the_user_id_with_429(
    apiClient: httpx.AsyncClient, createUser: CreateUser, testSettings: Settings
) -> None:
    """LOGIN_MAX_ATTEMPTS wrong passwords each give 401; then even the correct one gives 429."""
    user = createUser(REGULAR_USER_TYPE)
    await FailSignOns(apiClient, user.userId, user.password, testSettings.LOGIN_MAX_ATTEMPTS)
    response = await SignOn(apiClient, user.userId, user.password)
    AssertLegacyError(response, HTTP_TOO_MANY_REQUESTS, LOCKED_LITERAL, USER_ID_CURSOR)
    assert LOCKED_LITERAL == LOGIN_LOCKED_MESSAGE
    assert SET_COOKIE_HEADER not in response.headers


async def test_unknown_user_id_is_locked_after_max_failures(
    apiClient: httpx.AsyncClient, syncEngine: Engine, testSettings: Settings
) -> None:
    """The throttle counts the upper-cased ID, known or not, so its lower-case form is locked."""
    unknownUserId = NewUnknownUserId(syncEngine)
    for _attempt in range(testSettings.LOGIN_MAX_ATTEMPTS):
        response = await SignOn(apiClient, unknownUserId, NewCredential())
        AssertLegacyError(response, HTTP_UNAUTHORIZED, USER_NOT_FOUND_LITERAL, USER_ID_CURSOR)
    assert ReadFailureCount(syncEngine, unknownUserId) == testSettings.LOGIN_MAX_ATTEMPTS
    for lockedVariant in (unknownUserId, unknownUserId.lower()):
        response = await SignOn(apiClient, lockedVariant, NewCredential())
        AssertLegacyError(response, HTTP_TOO_MANY_REQUESTS, LOCKED_LITERAL, USER_ID_CURSOR)


async def test_success_resets_the_failure_count(
    apiClient: httpx.AsyncClient, createUser: CreateUser, syncEngine: Engine, testSettings: Settings
) -> None:
    """Failures one short of the limit, a success, then as many failures again never give 429.

    The success deletes the throttle row; each run of failures leaves its own count.
    """
    belowLimit = testSettings.LOGIN_MAX_ATTEMPTS - 1
    assert belowLimit > 0, "LOGIN_MAX_ATTEMPTS must allow a failure before the lock"
    user = createUser(REGULAR_USER_TYPE)
    await FailSignOns(apiClient, user.userId, user.password, belowLimit)
    assert ReadFailureCount(syncEngine, user.userId) == belowLimit
    signedOn = await SignOn(apiClient, user.userId, user.password)
    assert signedOn.status_code == HTTP_OK, signedOn.text
    assert ReadFailureCount(syncEngine, user.userId) is None
    await FailSignOns(apiClient, user.userId, user.password, belowLimit)
    assert ReadFailureCount(syncEngine, user.userId) == belowLimit


async def test_lock_expires_after_lockout_seconds(
    apiClient: httpx.AsyncClient,
    createUser: CreateUser,
    testSettings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the throttle clock passes LOGIN_LOCKOUT_SECONDS, the correct password signs on."""
    user = createUser(REGULAR_USER_TYPE)
    await FailSignOns(apiClient, user.userId, user.password, testSettings.LOGIN_MAX_ATTEMPTS)
    locked = await SignOn(apiClient, user.userId, user.password)
    AssertLegacyError(locked, HTTP_TOO_MANY_REQUESTS, LOCKED_LITERAL, USER_ID_CURSOR)
    realNow = login_throttle.UtcNow()
    expiredAt = realNow + timedelta(seconds=testSettings.LOGIN_LOCKOUT_SECONDS + 1)
    monkeypatch.setattr(login_throttle, "UtcNow", lambda: expiredAt)
    response = await SignOn(apiClient, user.userId, user.password)
    assert response.status_code == HTTP_OK, response.text


# --- Effective roles (AAP 0.7.1): admin is exactly A, every other stored type is regular --------
@pytest.mark.parametrize("userType", ROLE_TYPES)
async def test_me_reports_the_stored_user_type_and_its_own_menu(
    loginAs: LoginAs, userType: str
) -> None:
    """The type is never rewritten (u stays u); only A has /admin as its menu."""
    signedOn = await loginAs(userType)
    response = await signedOn.client.get(ME_URL)
    assert response.status_code == HTTP_OK, response.text
    assert response.json()["user_type"] == userType
    assert response.json()["next_route"] == ROLE_ROUTES[userType]


@pytest.mark.parametrize("endpointPath", ADMIN_ENDPOINTS)
@pytest.mark.parametrize("userType", ROLE_TYPES)
async def test_admin_endpoints_admit_only_type_a(
    loginAs: LoginAs, userType: str, endpointPath: str
) -> None:
    """CA00 and CU00 answer 200 for A; every other type gets 403 and the admin-only literal."""
    signedOn = await loginAs(userType)
    response = await signedOn.client.get(endpointPath)
    if userType == ADMIN_USER_TYPE:
        assert response.status_code == HTTP_OK, response.text
    else:
        AssertLegacyError(response, HTTP_FORBIDDEN, ADMIN_ONLY_LITERAL)


@pytest.mark.parametrize("endpointPath", REGULAR_ENDPOINTS)
@pytest.mark.parametrize("userType", ROLE_TYPES)
async def test_regular_endpoints_refuse_only_type_a(
    loginAs: LoginAs, userType: str, endpointPath: str
) -> None:
    """CM00 and CCLI answer 200 for U, u, a and X; an admin gets 403 with an empty message."""
    signedOn = await loginAs(userType)
    response = await signedOn.client.get(endpointPath)
    if userType == ADMIN_USER_TYPE:
        AssertLegacyError(response, HTTP_FORBIDDEN, NO_MESSAGE)
    else:
        assert response.status_code == HTTP_OK, response.text
