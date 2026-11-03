"""Replaces: COSGN00C (CC00), BMS COSGN00, copybooks CSUSR01Y, CSMSG01Y, COCOM01Y.

The CC00 sign-on. ENTER (PROCESS-ENTER-KEY) edits the two map fields in first-failure order: a
blank user ID, then a blank password, ends validation with its literal. Both inputs are then
upper-cased, letters a-z only as FUNCTION UPPER-CASE does, for the USRSEC read and the password
compare of READ-USER-SEC-FILE. Type exactly A opens /admin (XCTL COADM01C); every other type
opens /menu (XCTL COMEN01C). The login throttle is the one added control: a locked user ID
answers 429 before any edit, and every failed sign-on, the blank-field edits included, counts
one failure against the upper-cased ID. The blank-field edits, NOTFND and the wrong password
answer 401; the WHEN OTHER literal ``Unable to verify the User ...`` answers 503. The router
issues and clears the session cookie, so nothing here creates, returns or logs a token. PF3 of
MAIN-PARA is SignOff. MAIN-PARA's invalid-key branch has no path: the sign-on request carries no
AID key, and the COSGN00 legend offers only ENTER and F3.
"""

import asyncio
import string
from collections.abc import Mapping
from typing import Any, NamedTuple, NoReturn

from app.core.dependencies import CurrentUser, OnlineContext, SessionRefusal
from app.core.errors import (
    HTTP_FILE_ERROR,
    HTTP_SIGN_ON_FAILURE,
    LegacyMessageError,
    RecordNotFoundError,
    RepositoryError,
)
from app.core.messages import THANK_YOU_MESSAGE
from app.core.security import VerifyPassword
from app.repositories.user_repository import UserRepository
from app.schemas.auth import CurrentUserResponse, LoginRequest, LoginResponse, LogoutResponse
from app.services.auth import login_throttle

# Message literals of COSGN00C, verbatim (lines 120, 125, 242, 249, 254).
USER_ID_REQUIRED_MESSAGE = "Please enter User ID ..."
CREDENTIAL_REQUIRED_MESSAGE = "Please enter Password ..."
USER_NOT_FOUND_MESSAGE = "User not found. Try again ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."
UNABLE_TO_VERIFY_MESSAGE = "Unable to verify the User ..."

# The cursor fields that replace MOVE -1 TO USERIDL / PASSWDL, named as the LoginRequest fields.
USER_ID_FIELD = "user_id"
CREDENTIAL_FIELD = "password"
# The blank-field edits, NOTFND and the wrong password are sign-on failures and answer 401. The
# WHEN OTHER literal, which the batch window and every USRSEC or throttle failure give, is a
# file error and answers 503. login_throttle answers the lockout with 429.
SIGNON_FAILURE_STATUS = HTTP_SIGN_ON_FAILURE
UNABLE_TO_VERIFY_STATUS = HTTP_FILE_ERROR

# The pages that replace XCTL COADM01C and XCTL COMEN01C (lines 230-239).
ADMIN_ROUTE = "/admin"
MENU_ROUTE = "/menu"

# FUNCTION UPPER-CASE of an alphanumeric argument replaces only a-z and keeps the length;
# str.upper() would also change accented letters and expand some into two characters.
UPPER_CASE_TABLE = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
# The space padding of the PIC X(08) and X(20) fields: trailing spaces are never significant.
FIELD_PADDING = " "


class DisplayNames(NamedTuple):
    """SEC-USR-FNAME and SEC-USR-LNAME of the signed-on user, shown in the screen header."""

    firstName: str
    lastName: str


NO_DISPLAY_NAMES = DisplayNames(firstName="", lastName="")


def NormaliseSignOnInput(text: str) -> str:
    """Return a map field as COSGN00C keys it (lines 132-136): a-z upper-cased, padding trimmed.

    The trim stands for the X(08) space padding of WS-USER-ID and WS-USER-PWD: the stored key
    and the stored hash are of right-trimmed values. Leading spaces are kept.
    """
    return text.translate(UPPER_CASE_TABLE).rstrip(FIELD_PADDING)


def IsBlank(text: str) -> bool:
    """Return True for SPACES OR LOW-VALUES: an empty or omitted field, or one of spaces only."""
    return not text.strip(FIELD_PADDING)


def WithoutPadding(value: Any) -> str:
    """Return a stored field value as text without its trailing space padding."""
    return str(value).rstrip(FIELD_PADDING)


def SignOnError(message: str, field: str) -> LegacyMessageError:
    """Return the 401 sign-on failure carrying a COSGN00C literal and its cursor field."""
    return LegacyMessageError(message, field=field, httpStatus=SIGNON_FAILURE_STATUS)


def UnableToVerifyError() -> LegacyMessageError:
    """Return the 503 WHEN OTHER failure (lines 252-256): the literal, cursor on the user ID."""
    return LegacyMessageError(
        UNABLE_TO_VERIFY_MESSAGE, field=USER_ID_FIELD, httpStatus=UNABLE_TO_VERIFY_STATUS
    )


def RequiredFieldFailure(request: LoginRequest) -> LegacyMessageError | None:
    """Port the EVALUATE TRUE of PROCESS-ENTER-KEY (lines 117-130): the first blank field fails.

    Return the 401 failure of a blank user ID, else of a blank password, else None.
    """
    if IsBlank(request.user_id):
        return SignOnError(USER_ID_REQUIRED_MESSAGE, USER_ID_FIELD)
    if IsBlank(request.password):
        return SignOnError(CREDENTIAL_REQUIRED_MESSAGE, CREDENTIAL_FIELD)
    return None


def NextRouteFor(userType: str) -> str:
    """Return /admin for type exactly A (IF CDEMO-USRTYP-ADMIN, lines 230-239), else /menu.

    The stored type is tested as it is, never upper-cased or trimmed, so ``a``, ``u``, ``U``,
    ``X`` and a blank type are all regular users.
    """
    if CurrentUser(user_id="", user_type=userType).IsAdmin():
        return ADMIN_ROUTE
    return MENU_ROUTE


def BuildLoginResponse(userRow: Mapping[str, Any]) -> LoginResponse:
    """Return the signed-on user: the CDEMO-USER-ID and CDEMO-USER-TYPE moves (lines 226-227).

    The response carries no password, hash or token.
    """
    userType = userRow["user_type"]
    return LoginResponse(
        user_id=WithoutPadding(userRow["user_id"]),
        user_type=userType,
        first_name=WithoutPadding(userRow["first_name"]),
        last_name=WithoutPadding(userRow["last_name"]),
        next_route=NextRouteFor(userType),
    )


async def FailSignOn(
    context: OnlineContext, upperUserId: str, failure: LegacyMessageError
) -> NoReturn:
    """Count one failed sign-on for the upper-cased user ID, then raise its ``failure``.

    The failure commits in the throttle's own write unit before the error reaches the client.
    A throttle that cannot record it answers the 503 WHEN OTHER literal instead, so the control
    never fails open.
    """
    try:
        await login_throttle.RecordFailure(context, upperUserId)
    except RepositoryError as error:
        raise UnableToVerifyError() from error
    raise failure


async def EditRequiredFields(
    context: OnlineContext, upperUserId: str, request: LoginRequest
) -> None:
    """Fail the first blank map field, counting it like every other failed sign-on.

    The failure counts against the upper-cased user ID, so a blank password counts against the
    keyed ID and a blank user ID against the blank ID.
    """
    blankFieldFailure = RequiredFieldFailure(request)
    if blankFieldFailure is not None:
        await FailSignOn(context, upperUserId, blankFieldFailure)


async def GuardLockout(context: OnlineContext, upperUserId: str) -> None:
    """Refuse a locked user ID with the throttle's 429 before any edit runs.

    A throttle read failure is the 503 WHEN OTHER literal and counts no failure.
    """
    try:
        await login_throttle.CheckLocked(context, upperUserId)
    except RepositoryError as error:
        raise UnableToVerifyError() from error


async def ClearFailures(context: OnlineContext, upperUserId: str) -> None:
    """Delete the user ID's failure count after a successful sign-on.

    A throttle that cannot reset answers the 503 WHEN OTHER literal instead of signing on.
    """
    try:
        await login_throttle.ResetAttempts(context, upperUserId)
    except RepositoryError as error:
        raise UnableToVerifyError() from error


async def LookUpUser(context: OnlineContext, upperUserId: str) -> dict[str, Any] | None:
    """Port the CICS READ of USRSEC (lines 211-219): the record, or None for NOTFND (RESP 13).

    Any other condition, the batch window's NOTOPEN included, is the WHEN OTHER branch
    (lines 252-256): it counts a failure and answers the 503 literal.
    """
    try:
        return await UserRepository(context).GetUser(upperUserId)
    except RecordNotFoundError:
        return None
    except RepositoryError:
        await FailSignOn(context, upperUserId, UnableToVerifyError())


async def CredentialMatches(upperCredential: str, passwordHash: str) -> bool:
    """Return whether the upper-cased password verifies against the stored bcrypt hash.

    bcrypt runs in a worker thread so it never blocks the event loop. VerifyPassword already
    answers False for a blank or malformed stored hash, as a corrupt SEC-USR-PWD simply fails
    the legacy compare.
    """
    return await asyncio.to_thread(VerifyPassword, upperCredential, passwordHash)


async def VerifyUser(
    context: OnlineContext, upperUserId: str, upperCredential: str
) -> Mapping[str, Any]:
    """Port EVALUATE WS-RESP-CD of READ-USER-SEC-FILE (lines 221-257); return the user record.

    NOTFND gives ``User not found. Try again ...`` and a password that does not verify gives
    ``Wrong Password. Try again ...``; each counts one failure.
    """
    userRow = await LookUpUser(context, upperUserId)
    if userRow is None:
        await FailSignOn(context, upperUserId, SignOnError(USER_NOT_FOUND_MESSAGE, USER_ID_FIELD))
    if not await CredentialMatches(upperCredential, userRow["password_hash"]):
        wrongCredential = SignOnError(WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD)
        await FailSignOn(context, upperUserId, wrongCredential)
    return userRow


async def SignOn(context: OnlineContext, request: LoginRequest) -> LoginResponse:
    """Port PROCESS-ENTER-KEY (lines 108-140) and READ-USER-SEC-FILE (lines 209-257).

    Raises:
        LegacyMessageError: 429 with the lockout literal while the user ID is locked; 503 with
            ``Unable to verify the User ...`` when USRSEC or the throttle fails, the batch
            window included; 401 with the COSGN00C literal and its cursor field for every other
            sign-on failure.
    """
    upperUserId = NormaliseSignOnInput(request.user_id)
    upperCredential = NormaliseSignOnInput(request.password)
    await GuardLockout(context, upperUserId)
    await EditRequiredFields(context, upperUserId, request)
    userRow = await VerifyUser(context, upperUserId, upperCredential)
    await ClearFailures(context, upperUserId)
    return BuildLoginResponse(userRow)


def SignOff() -> LogoutResponse:
    """Port the PF3 branch of MAIN-PARA (lines 88-90): the CSMSG01Y CCDA-MSG-THANK-YOU literal."""
    return LogoutResponse(message=THANK_YOU_MESSAGE)


async def ReadDisplayNames(context: OnlineContext, userId: str) -> DisplayNames:
    """Return the user's names for the screen header, or blank names when USRSEC cannot say.

    The names are display only. A deleted user keeps a valid session until the next sign-on,
    and the menus touch no batch-window file, so NOTFND and every other file condition, NOTOPEN
    included, give blank names rather than an error.
    """
    try:
        userRow = await UserRepository(context).GetUser(userId)
    except RepositoryError:
        return NO_DISPLAY_NAMES
    if userRow is None:
        return NO_DISPLAY_NAMES
    return DisplayNames(
        firstName=WithoutPadding(userRow["first_name"]),
        lastName=WithoutPadding(userRow["last_name"]),
    )


async def DescribeCurrentUser(context: OnlineContext) -> CurrentUserResponse:
    """Return the session user of GET /auth/me: the COMMAREA CDEMO-USER-ID and CDEMO-USER-TYPE.

    The user ID and type come from the verified session token, which holds for its lifetime;
    only the display names are read from USRSEC. A context without a user is refused with 401.
    """
    sessionUser = context.user
    if sessionUser is None:
        raise SessionRefusal()
    displayNames = await ReadDisplayNames(context, sessionUser.user_id)
    return CurrentUserResponse(
        user_id=sessionUser.user_id,
        user_type=sessionUser.user_type,
        first_name=displayNames.firstName,
        last_name=displayNames.lastName,
        next_route=NextRouteFor(sessionUser.user_type),
    )
