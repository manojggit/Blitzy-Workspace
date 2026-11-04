"""Password hashing, the session token, encrypted navigation tokens, and PAN and SSN masking.

Replaces: RACF sign-on and the plaintext SEC-USR-PWD compare (CSUSR01Y, COSGN00C), COMMAREA
identity (COCOM01Y), the COMMAREA card key passed by COCRDLIC, and the COACTUPC/COCRDUPC
COMMAREA screen state. Specified in the Technical Specification, Section 0.7.1.

- Passwords: a bcrypt hash of the text as entered replaces SEC-USR-PWD X(08). COUSR01C and
  COUSR02C keep the case; COSGN00C upper-cases sign-on input before calling VerifyPassword.
- Session token: an HS256 JWT signed with SECRET_KEY, carried only in the HttpOnly session
  cookie. Its claims sub and typ replace CDEMO-USER-ID and CDEMO-USER-TYPE.
- card_ref: a Fernet token carrying the CDEMO-CARD-NUM that COCRDLIC passed to COCRDSLC and
  COCRDUPC, so card numbers never appear in frontend URLs.
- screen_token: a Fernet token carrying the ACUP-*/CCUP-* change action, keys, OLD-*
  before-image and validated new values that the CAUP and CCUP COMMAREA held between screens.
- Masking: PAN as twelve asterisks plus the last four, SSN as ***-**- plus the last four.

Importing this module reads no setting and no environment variable: callers pass the Settings
(or the bcrypt rounds) to each function that uses them. Batch loaders, the sample seed and
Alembic revision 0002 therefore call HashPassword with no SECRET_KEY configured.
"""

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from passlib.context import CryptContext

if TYPE_CHECKING:
    from app.core.config import Settings

HASH_SCHEME = "bcrypt"
PASSWORD_CONTEXT = CryptContext(schemes=[HASH_SCHEME], deprecated="auto")
SESSION_SIGNING_ALGORITHM = "HS256"
SESSION_REQUIRED_CLAIMS = ("sub", "typ", "iat", "exp")
CARD_REF_HKDF_INFO = b"carddemo-card-ref"
SCREEN_STATE_HKDF_INFO = b"carddemo-screen"
DERIVED_KEY_LENGTH = 32
SECONDS_PER_MINUTE = 60
MASKED_PAN_PREFIX = "************"
MASKED_SSN_PREFIX = "***-**-"
UNMASKED_CHARACTER_COUNT = 4


def HashPassword(password: str, rounds: int) -> str:
    """Return a bcrypt hash of password exactly as entered, at cost rounds (BCRYPT_ROUNDS)."""
    # CryptContext.using() takes context options only and rejects "rounds", so the cost is
    # set on the context's bcrypt handler instead.
    return PASSWORD_CONTEXT.handler(HASH_SCHEME).using(rounds=rounds).hash(password)


def VerifyPassword(password: str, passwordHash: str) -> bool:
    """Return True when password, exactly as entered, matches the stored passwordHash."""
    try:
        return PASSWORD_CONTEXT.verify(password, passwordHash)
    except ValueError:
        # A blank, malformed or non-bcrypt stored hash matches no password.
        return False


@dataclass(frozen=True)
class SessionClaims:
    """Identity read from a verified session token: CDEMO-USER-ID and CDEMO-USER-TYPE."""

    userId: str
    userType: str


def IssueSessionToken(userId: str, userType: str, settings: "Settings") -> str:
    """Return an HS256 session token for the user, valid for ACCESS_TOKEN_EXPIRE_MINUTES."""
    issuedAt = datetime.now(UTC)
    expiresAt = issuedAt + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    claims = {"sub": userId, "typ": userType, "iat": issuedAt, "exp": expiresAt}
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=SESSION_SIGNING_ALGORITHM)


def ReadSessionToken(token: str, settings: "Settings") -> SessionClaims:
    """Return the identity in a valid session token; raise jwt.InvalidTokenError otherwise.

    A token whose header is nested too deeply to parse is refused as InvalidTokenError too:
    PyJWT parses the unverified header with json.loads and turns only its ValueError into a
    DecodeError, so the RecursionError of an over-nested header is converted here.
    """
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[SESSION_SIGNING_ALGORITHM],
            options={"require": list(SESSION_REQUIRED_CLAIMS)},
        )
    except RecursionError:
        raise jwt.InvalidTokenError("token nested too deeply to parse") from None
    userType = claims["typ"]
    if not isinstance(userType, str):
        raise jwt.InvalidTokenError("invalid typ claim")
    return SessionClaims(userId=claims["sub"], userType=userType)


def SessionCookieOptions(settings: "Settings") -> dict[str, object]:
    """Return the Starlette Response.set_cookie keyword arguments of the session cookie."""
    return {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max_age": _TokenLifetimeSeconds(settings),
        "secure": settings.IsProduction(),
    }


def DeriveFernet(secretKey: str, info: bytes) -> Fernet:
    """Return the Fernet cipher whose key HKDF-SHA256 derives from secretKey and info."""
    keyDerivation = HKDF(algorithm=hashes.SHA256(), length=DERIVED_KEY_LENGTH, salt=None, info=info)
    derivedKey = keyDerivation.derive(secretKey.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derivedKey))


def EncodeCardRef(cardNum: str, settings: "Settings") -> str:
    """Return the card_ref token that carries cardNum in place of COMMAREA CDEMO-CARD-NUM."""
    cardRefCipher = DeriveFernet(settings.SECRET_KEY, CARD_REF_HKDF_INFO)
    return cardRefCipher.encrypt(cardNum.encode("utf-8")).decode("ascii")


def DecodeCardRef(cardRef: str, settings: "Settings") -> str:
    """Return the card number in cardRef; raise InvalidToken when it is invalid or expired."""
    return _DecryptWithLifetime(cardRef, CARD_REF_HKDF_INFO, settings).decode("utf-8")


def EncodeScreenToken(state: Mapping[str, object], settings: "Settings") -> str:
    """Return the screen_token carrying the CAUP or CCUP screen state as compact JSON."""
    stateText = json.dumps(dict(state), sort_keys=True, separators=(",", ":"))
    screenCipher = DeriveFernet(settings.SECRET_KEY, SCREEN_STATE_HKDF_INFO)
    return screenCipher.encrypt(stateText.encode("utf-8")).decode("ascii")


def DecodeScreenToken(screenToken: str, settings: "Settings") -> dict[str, object]:
    """Return the screen state in screenToken; raise InvalidToken when invalid or expired."""
    stateText = _DecryptWithLifetime(screenToken, SCREEN_STATE_HKDF_INFO, settings)
    state = json.loads(stateText)
    if not isinstance(state, dict):
        raise InvalidToken
    return state


def MaskCardNumber(cardNum: str) -> str:
    """Return cardNum as twelve asterisks plus its last four characters, or "" when blank."""
    return _MaskAllButLastCharacters(cardNum, MASKED_PAN_PREFIX)


def MaskSsn(ssn: str) -> str:
    """Return ssn as ***-**- plus its last four characters, or "" when blank."""
    return _MaskAllButLastCharacters(ssn, MASKED_SSN_PREFIX)


def _TokenLifetimeSeconds(settings: "Settings") -> int:
    """Return ACCESS_TOKEN_EXPIRE_MINUTES in seconds: the cookie and Fernet token lifetime."""
    return settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE


def _DecryptWithLifetime(token: str, info: bytes, settings: "Settings") -> bytes:
    """Return the plaintext of a Fernet token for info; raise InvalidToken when not valid."""
    # Fernet raises a plain ValueError rather than InvalidToken for a str holding non-ASCII
    # characters, and request fields allow the code-page-037 repertoire, which has some.
    if not token.isascii():
        raise InvalidToken
    tokenCipher = DeriveFernet(settings.SECRET_KEY, info)
    return tokenCipher.decrypt(token, ttl=_TokenLifetimeSeconds(settings))


def _MaskAllButLastCharacters(value: str, maskedPrefix: str) -> str:
    """Return maskedPrefix plus the last four characters of value trimmed, or "" when blank."""
    trimmedValue = value.strip()
    if not trimmedValue:
        return ""
    return maskedPrefix + trimmedValue[-UNMASKED_CHARACTER_COUNT:]
