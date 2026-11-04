"""Unit tests for app.core.security: passwords, the session token, navigation tokens, masking.

The unit replaces RACF sign-on and the plaintext SEC-USR-PWD compare (cpy/CSUSR01Y.cpy,
cbl/COSGN00C.cbl), the COMMAREA identity CDEMO-USER-ID and CDEMO-USER-TYPE (cpy/COCOM01Y.cpy),
the COMMAREA card key CDEMO-CARD-NUM that cbl/COCRDLIC.cbl passes on, and the CAUP and CCUP
COMMAREA screen state, as specified in the Technical Specification, Section 0.7.1. The session
cookie whose options are pinned here is the one sign-off clears with the CSMSG01Y thank-you
literal (cpy/CSMSG01Y.cpy).

These tests pin that:

- a password hash is a salted bcrypt hash at the requested cost, matching only the text as
  entered, case kept, and an unusable stored hash matches no password;
- the session token is HS256 over SECRET_KEY, carries sub, typ, iat and exp for exactly
  ACCESS_TOKEN_EXPIRE_MINUTES and the stored user type unchanged, and is refused when expired,
  signed with another key, tampered with, forged to the admin type, missing a claim, typed
  wrongly, or nested too deeply to parse, the last with the legacy 401 and an empty message
  instead of a 500;
- the cookie options are HttpOnly, SameSite=Lax, Path=/ and the session lifetime, Secure only
  in production, and build the Set-Cookie header through Starlette;
- card_ref and screen_token round-trip, hide their plain text, expire with the session, and are
  refused when tampered with, non-ASCII, encrypted under another key or presented as the other
  kind, and a screen state that is not a JSON object is refused;
- a PAN and an SSN are masked to their last four characters, and a blank value to "".

Every key and password is generated at run time with the secrets module, backend/.env is never
read (_env_file=None), and no test touches the database.
"""

import base64
import json
import secrets
import string
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import jwt
import pytest
from cryptography import fernet
from cryptography.fernet import InvalidToken
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings
from app.core.dependencies import GetCurrentUser
from app.core.errors import STANDARD_BODY_KEYS, LegacyMessageError
from app.core.security import (
    SCREEN_STATE_HKDF_INFO,
    DecodeCardRef,
    DecodeScreenToken,
    DeriveFernet,
    EncodeCardRef,
    EncodeScreenToken,
    HashPassword,
    IssueSessionToken,
    MaskCardNumber,
    MaskSsn,
    ReadSessionToken,
    SessionClaims,
    SessionCookieOptions,
    VerifyPassword,
)

# Far past the JSON decoder's nesting limit (about 10,000 levels on CPython 3.13.7), so
# parsing the header raises RecursionError rather than ValueError.
NESTED_HEADER_DEPTH = 20000
SESSION_REFUSAL_STATUS = 401

# Public sample values: a sample-data card number, an SSN and a USRSEC user ID.
SAMPLE_PAN = "4859452612877065"
SAMPLE_SSN = "123456789"
SAMPLE_USER_ID = "USER0001"
SAMPLE_ACCT_ID = "00000000004"
SAMPLE_SCREEN_STATE = {
    "state": "SHOW_DETAILS",
    "acct_id": SAMPLE_ACCT_ID,
    "old": {"credit_limit": "1000.00"},
}
# A PIC X(16) card-number field holding only spaces.
BLANK_FIELD = " " * 16
NON_ASCII_CHARACTER = "\u00e9"
# JSON texts that decrypt under the screen key but hold no screen-state object.
NON_OBJECT_STATE_TEXTS = ("[]", '"SHOW_DETAILS"', "0", "null")

TEST_BCRYPT_ROUNDS = 4
BCRYPT_COST_PREFIX = f"$2b${TEST_BCRYPT_ROUNDS:02d}$"
PASSWORD_TOKEN_BYTES = 16
SECRET_KEY_BYTES = 32
DATABASE_PASSWORD_BYTES = 12

ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# A stored type the token carries unchanged: only exactly "A" is the admin type.
LOWER_CASE_ADMIN_TYPE = "a"
SESSION_ALGORITHM = "HS256"
SESSION_CLAIM_NAMES = frozenset({"sub", "typ", "iat", "exp"})
SECONDS_PER_MINUTE = 60
# The lifetime of hand-built session tokens, and how far short of or past its deadline a
# Fernet token is issued.
CLOCK_MARGIN_SECONDS = 60

LOCAL_APP_ENV = "local"
TEST_APP_ENV = "test"
PRODUCTION_APP_ENV = "production"
COOKIE_ENVIRONMENTS = (
    pytest.param(LOCAL_APP_ENV, False, id="local"),
    pytest.param(TEST_APP_ENV, False, id="test"),
    pytest.param(PRODUCTION_APP_ENV, True, id="production"),
)

URL_SAFE_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
URL_SAFE_DIGITS = frozenset(URL_SAFE_ALPHABET)


@dataclass(frozen=True)
class FernetTokenKind:
    """One Fernet navigation token kind: its codec, a sample payload and text it must hide."""

    encoder: Callable[[object, Settings], str]
    decoder: Callable[[str, Settings], object]
    samplePayload: object
    hiddenText: str

    def Encode(self, settings):
        """Return a token of this kind carrying samplePayload under the key of settings."""
        return self.encoder(self.samplePayload, settings)

    def Decode(self, tokenText, settings):
        """Return the payload of tokenText; raise InvalidToken when it is not valid."""
        return self.decoder(tokenText, settings)


CARD_REF_KIND = FernetTokenKind(EncodeCardRef, DecodeCardRef, SAMPLE_PAN, SAMPLE_PAN)
SCREEN_TOKEN_KIND = FernetTokenKind(
    EncodeScreenToken, DecodeScreenToken, SAMPLE_SCREEN_STATE, SAMPLE_ACCT_ID
)
FERNET_TOKEN_KINDS = (
    pytest.param(CARD_REF_KIND, id="card_ref"),
    pytest.param(SCREEN_TOKEN_KIND, id="screen_token"),
)
CROSSED_TOKEN_KINDS = (
    pytest.param(CARD_REF_KIND, SCREEN_TOKEN_KIND, id="card_ref_read_as_screen_token"),
    pytest.param(SCREEN_TOKEN_KIND, CARD_REF_KIND, id="screen_token_read_as_card_ref"),
)
MASKING_CASES = (
    pytest.param(MaskCardNumber, SAMPLE_PAN, "************7065", id="pan"),
    pytest.param(MaskSsn, SAMPLE_SSN, "***-**-6789", id="ssn"),
    pytest.param(MaskCardNumber, BLANK_FIELD, "", id="blank_pan"),
    pytest.param(MaskSsn, BLANK_FIELD, "", id="blank_ssn"),
)


def BuildDatabaseUrl(driverName, databasePassword):
    """Return a PostgreSQL URL on host 127.0.0.1 whose credentials carry databasePassword."""
    return f"postgresql+{driverName}://security_test:{databasePassword}@127.0.0.1/security_test"


def BuildSettings(appEnv):
    """Return backend Settings for appEnv with a generated SECRET_KEY and database password."""
    databasePassword = secrets.token_urlsafe(DATABASE_PASSWORD_BYTES)
    return Settings(
        _env_file=None,
        APP_ENV=appEnv,
        SECRET_KEY=secrets.token_urlsafe(SECRET_KEY_BYTES),
        DATABASE_URL=BuildDatabaseUrl("asyncpg", databasePassword),
        SYNC_DATABASE_URL=BuildDatabaseUrl("psycopg2", databasePassword),
    )


def SessionLifetimeSeconds(settings):
    """Return ACCESS_TOKEN_EXPIRE_MINUTES of settings in seconds."""
    return settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE


def NewMixedCasePassword():
    """Return a generated password holding at least one upper-case letter."""
    password = secrets.token_urlsafe(PASSWORD_TOKEN_BYTES)
    while password == password.lower():
        password = secrets.token_urlsafe(PASSWORD_TOKEN_BYTES)
    return password


def TamperToken(tokenText):
    """Return tokenText with one base64url digit at or after its middle replaced by another.

    The digit replaced is always followed by another digit: the last digit of a JWT segment,
    or the one before Fernet padding, may carry unused low bits and decode unchanged.
    """
    tamperIndex = len(tokenText) // 2
    while not set(tokenText[tamperIndex : tamperIndex + 2]) <= URL_SAFE_DIGITS:
        tamperIndex += 1
    replacement = URL_SAFE_ALPHABET.replace(tokenText[tamperIndex], "")[0]
    return tokenText[:tamperIndex] + replacement + tokenText[tamperIndex + 1 :]


def EncodeSegment(segmentBytes):
    """Return segmentBytes as an unpadded base64url JWT segment."""
    return base64.urlsafe_b64encode(segmentBytes).rstrip(b"=").decode("ascii")


def ForgePayload(sessionToken, forgedClaims):
    """Return sessionToken with its payload replaced by forgedClaims and its signature kept."""
    headerSegment, _, signatureSegment = sessionToken.split(".")
    payloadSegment = EncodeSegment(json.dumps(forgedClaims).encode("utf-8"))
    return f"{headerSegment}.{payloadSegment}.{signatureSegment}"


def ClaimsIssuedAt(issuedAt):
    """Return regular-user session claims issued at issuedAt, valid CLOCK_MARGIN_SECONDS."""
    return {
        "sub": SAMPLE_USER_ID,
        "typ": REGULAR_USER_TYPE,
        "iat": issuedAt,
        "exp": issuedAt + CLOCK_MARGIN_SECONDS,
    }


def SignClaims(sessionClaims, settings):
    """Return sessionClaims signed with HS256 under the SECRET_KEY of settings."""
    return jwt.encode(sessionClaims, settings.SECRET_KEY, algorithm=SESSION_ALGORITHM)


def EncodeAtAge(tokenKind, settings, ageSeconds):
    """Return a token of tokenKind as Fernet would have issued it ageSeconds ago.

    Only the clock cryptography.fernet reads is moved, and only while the token is encrypted.
    """
    issuedAt = time.time() - ageSeconds
    with pytest.MonkeyPatch.context() as clockPatch:
        clockPatch.setattr(fernet, "time", SimpleNamespace(time=lambda: issuedAt))
        return tokenKind.Encode(settings)


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
    """Return test-environment backend Settings holding a generated SECRET_KEY."""
    return BuildSettings(TEST_APP_ENV)


@pytest.fixture(name="otherKeySettings")
def OtherKeySettings():
    """Return test-environment backend Settings holding a second, different SECRET_KEY."""
    return BuildSettings(TEST_APP_ENV)


@pytest.fixture(name="nestedHeaderToken")
def NestedHeaderToken():
    """Return a token with the deeply nested header, an empty-object payload and no signature."""
    headerSegment = EncodeSegment(BuildNestedHeaderText(NESTED_HEADER_DEPTH).encode("ascii"))
    return f"{headerSegment}.{EncodeSegment(b'{}')}."


# --- Passwords -----------------------------------------------------------------------------------
def test_hash_password_produces_salted_bcrypt_hash():
    password = NewMixedCasePassword()
    firstHash = HashPassword(password, TEST_BCRYPT_ROUNDS)
    secondHash = HashPassword(password, TEST_BCRYPT_ROUNDS)
    assert firstHash.startswith(BCRYPT_COST_PREFIX)
    assert password not in firstHash
    assert firstHash != secondHash


def test_verify_password_accepts_same_text_only():
    password = NewMixedCasePassword()
    passwordHash = HashPassword(password, TEST_BCRYPT_ROUNDS)
    assert VerifyPassword(password, passwordHash) is True
    assert VerifyPassword(NewMixedCasePassword(), passwordHash) is False
    assert VerifyPassword(password.lower(), passwordHash) is False


@pytest.mark.parametrize(
    "storedHash",
    [
        pytest.param("", id="blank"),
        pytest.param("not-a-bcrypt-hash", id="unidentified"),
        pytest.param(BCRYPT_COST_PREFIX + "truncated", id="truncated_bcrypt"),
    ],
)
def test_verify_password_refuses_unusable_stored_hash(storedHash):
    assert VerifyPassword(NewMixedCasePassword(), storedHash) is False


# --- Session token -------------------------------------------------------------------------------
@pytest.mark.parametrize("userType", [ADMIN_USER_TYPE, REGULAR_USER_TYPE, LOWER_CASE_ADMIN_TYPE])
def test_session_token_round_trip(userType, sessionSettings):
    sessionToken = IssueSessionToken(SAMPLE_USER_ID, userType, sessionSettings)
    sessionClaims = ReadSessionToken(sessionToken, sessionSettings)
    assert sessionClaims == SessionClaims(userId=SAMPLE_USER_ID, userType=userType)


def test_session_token_lifetime_matches_setting(sessionSettings):
    sessionToken = IssueSessionToken(SAMPLE_USER_ID, REGULAR_USER_TYPE, sessionSettings)
    tokenClaims = jwt.decode(
        sessionToken, sessionSettings.SECRET_KEY, algorithms=[SESSION_ALGORITHM]
    )
    assert set(tokenClaims) == SESSION_CLAIM_NAMES
    assert tokenClaims["exp"] - tokenClaims["iat"] == SessionLifetimeSeconds(sessionSettings)
    assert jwt.get_unverified_header(sessionToken)["alg"] == SESSION_ALGORITHM


def test_expired_session_token_is_refused(sessionSettings):
    expiredClaims = ClaimsIssuedAt(int(time.time()) - 2 * CLOCK_MARGIN_SECONDS)
    expiredToken = SignClaims(expiredClaims, sessionSettings)
    with pytest.raises(jwt.ExpiredSignatureError):
        ReadSessionToken(expiredToken, sessionSettings)


def test_session_token_from_other_key_is_refused(sessionSettings, otherKeySettings):
    foreignToken = IssueSessionToken(SAMPLE_USER_ID, REGULAR_USER_TYPE, otherKeySettings)
    with pytest.raises(jwt.InvalidSignatureError):
        ReadSessionToken(foreignToken, sessionSettings)


def test_tampered_session_token_is_refused(sessionSettings):
    sessionToken = IssueSessionToken(SAMPLE_USER_ID, REGULAR_USER_TYPE, sessionSettings)
    tamperedToken = TamperToken(sessionToken)
    assert tamperedToken != sessionToken
    with pytest.raises(jwt.InvalidTokenError):
        ReadSessionToken(tamperedToken, sessionSettings)


def test_session_token_forged_to_admin_type_is_refused(sessionSettings):
    sessionToken = IssueSessionToken(SAMPLE_USER_ID, REGULAR_USER_TYPE, sessionSettings)
    forgedClaims = jwt.decode(
        sessionToken, sessionSettings.SECRET_KEY, algorithms=[SESSION_ALGORITHM]
    )
    forgedClaims["typ"] = ADMIN_USER_TYPE
    forgedToken = ForgePayload(sessionToken, forgedClaims)
    unverifiedClaims = jwt.decode(forgedToken, options={"verify_signature": False})
    assert unverifiedClaims["typ"] == ADMIN_USER_TYPE
    with pytest.raises(jwt.InvalidSignatureError):
        ReadSessionToken(forgedToken, sessionSettings)


@pytest.mark.parametrize("missingClaim", sorted(SESSION_CLAIM_NAMES))
def test_session_token_missing_claim_is_refused(missingClaim, sessionSettings):
    incompleteClaims = ClaimsIssuedAt(int(time.time()))
    del incompleteClaims[missingClaim]
    incompleteToken = SignClaims(incompleteClaims, sessionSettings)
    with pytest.raises(jwt.MissingRequiredClaimError):
        ReadSessionToken(incompleteToken, sessionSettings)


def test_session_token_with_non_text_user_type_is_refused(sessionSettings):
    numericTypeClaims = ClaimsIssuedAt(int(time.time()))
    numericTypeClaims["typ"] = 1
    numericTypeToken = SignClaims(numericTypeClaims, sessionSettings)
    with pytest.raises(jwt.InvalidTokenError, match="typ"):
        ReadSessionToken(numericTypeToken, sessionSettings)


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


# --- Session cookie options ----------------------------------------------------------------------
@pytest.mark.parametrize(("appEnv", "secureExpected"), COOKIE_ENVIRONMENTS)
def test_cookie_options_follow_app_env(appEnv, secureExpected):
    cookieSettings = BuildSettings(appEnv)
    assert SessionCookieOptions(cookieSettings) == {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max_age": SessionLifetimeSeconds(cookieSettings),
        "secure": secureExpected,
    }


@pytest.mark.parametrize(("appEnv", "secureExpected"), COOKIE_ENVIRONMENTS)
def test_cookie_options_build_the_set_cookie_header(appEnv, secureExpected):
    cookieSettings = BuildSettings(appEnv)
    response = Response()
    cookieName = cookieSettings.SESSION_COOKIE_NAME
    response.set_cookie(cookieName, SAMPLE_USER_ID, **SessionCookieOptions(cookieSettings))
    cookieAttributes = response.headers["set-cookie"].split("; ")
    maxAgeAttribute = f"Max-Age={SessionLifetimeSeconds(cookieSettings)}"
    assert cookieAttributes[0] == f"{cookieName}={SAMPLE_USER_ID}"
    assert {"HttpOnly", "Path=/", "SameSite=lax", maxAgeAttribute} <= set(cookieAttributes)
    assert ("Secure" in cookieAttributes) is secureExpected


# --- Fernet navigation tokens: card_ref and screen_token -----------------------------------------
@pytest.mark.parametrize("tokenKind", FERNET_TOKEN_KINDS)
def test_fernet_token_round_trip(tokenKind, sessionSettings):
    tokenText = tokenKind.Encode(sessionSettings)
    assert tokenKind.hiddenText not in tokenText
    assert tokenKind.Decode(tokenText, sessionSettings) == tokenKind.samplePayload


@pytest.mark.parametrize("tokenKind", FERNET_TOKEN_KINDS)
def test_tampered_fernet_token_is_refused(tokenKind, sessionSettings):
    tokenText = tokenKind.Encode(sessionSettings)
    tamperedToken = TamperToken(tokenText)
    assert tamperedToken != tokenText
    with pytest.raises(InvalidToken):
        tokenKind.Decode(tamperedToken, sessionSettings)


@pytest.mark.parametrize("tokenKind", FERNET_TOKEN_KINDS)
def test_non_ascii_fernet_token_is_refused(tokenKind, sessionSettings):
    nonAsciiToken = tokenKind.Encode(sessionSettings) + NON_ASCII_CHARACTER
    with pytest.raises(InvalidToken):
        tokenKind.Decode(nonAsciiToken, sessionSettings)


@pytest.mark.parametrize("tokenKind", FERNET_TOKEN_KINDS)
def test_fernet_token_from_other_key_is_refused(tokenKind, sessionSettings, otherKeySettings):
    foreignToken = tokenKind.Encode(otherKeySettings)
    with pytest.raises(InvalidToken):
        tokenKind.Decode(foreignToken, sessionSettings)


@pytest.mark.parametrize(("issuedKind", "presentedKind"), CROSSED_TOKEN_KINDS)
def test_fernet_token_of_other_kind_is_refused(issuedKind, presentedKind, sessionSettings):
    issuedToken = issuedKind.Encode(sessionSettings)
    with pytest.raises(InvalidToken):
        presentedKind.Decode(issuedToken, sessionSettings)


@pytest.mark.parametrize("tokenKind", FERNET_TOKEN_KINDS)
def test_fernet_token_expires_with_the_session(tokenKind, sessionSettings):
    tokenLifetime = SessionLifetimeSeconds(sessionSettings)
    youngToken = EncodeAtAge(tokenKind, sessionSettings, tokenLifetime - CLOCK_MARGIN_SECONDS)
    expiredToken = EncodeAtAge(tokenKind, sessionSettings, tokenLifetime + CLOCK_MARGIN_SECONDS)
    assert fernet.time is time
    assert tokenKind.Decode(youngToken, sessionSettings) == tokenKind.samplePayload
    with pytest.raises(InvalidToken):
        tokenKind.Decode(expiredToken, sessionSettings)


@pytest.mark.parametrize("stateText", NON_OBJECT_STATE_TEXTS)
def test_screen_token_without_state_object_is_refused(stateText, sessionSettings):
    screenCipher = DeriveFernet(sessionSettings.SECRET_KEY, SCREEN_STATE_HKDF_INFO)
    screenToken = screenCipher.encrypt(stateText.encode("utf-8")).decode("ascii")
    with pytest.raises(InvalidToken):
        DecodeScreenToken(screenToken, sessionSettings)


# --- Masking -------------------------------------------------------------------------------------
@pytest.mark.parametrize(("maskFunction", "rawValue", "maskedValue"), MASKING_CASES)
def test_masking_keeps_only_last_four_characters(maskFunction, rawValue, maskedValue):
    assert maskFunction(rawValue) == maskedValue
