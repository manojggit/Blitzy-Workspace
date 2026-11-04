"""Replaces: COSGN00C (CC00), BMS COSGN00, copybooks CSUSR01Y, CSMSG01Y, COCOM01Y.

The CC00 sign-on. ENTER (PROCESS-ENTER-KEY) edits the two map fields in first-failure order: a
blank user ID, then a blank password, ends validation with its literal. Both inputs are then
upper-cased, letters a-z only as FUNCTION UPPER-CASE does, for the USRSEC read and the password
compare of READ-USER-SEC-FILE. Type exactly A opens /admin (XCTL COADM01C); every other type
opens /menu (XCTL COMEN01C). The login throttle is the one added control, keyed by the
upper-cased user ID. A blank user ID reads no USRSEC record and names no key, so its edit
answers before the throttle: it is never counted or refused with 429. Every other attempt then
waits for a check slot of the upper-cased ID before the password edit: a locked user ID answers
429, and the failures counted since the last reset plus the checks in flight never exceed
LOGIN_MAX_ATTEMPTS, so an attempt over the limit waits for the outcomes in flight instead of
answering 429. Every admitted attempt that does not sign on, the blank-password edit and the
503 included, counts one failure before it answers; a successful one clears the count. The
blank-field edits, NOTFND and the wrong password answer 401; the WHEN OTHER literal
``Unable to verify the User ...`` answers 503. The router issues and clears the
session cookie, so nothing here creates, returns or logs a token. PF3 of MAIN-PARA is SignOff.
MAIN-PARA's invalid-key branch has no path: the sign-on request carries no AID key, and the
COSGN00 legend offers only ENTER and F3.
"""

import asyncio
import logging
import string
from collections.abc import Mapping
from typing import Any, NamedTuple

from app.core.dependencies import CurrentUser, OnlineContext, SessionRefusal
from app.core.errors import (
    HTTP_FILE_ERROR,
    HTTP_SIGN_ON_FAILURE,
    LegacyMessageError,
    LogFileCondition,
    RecordNotFoundError,
    RepositoryError,
)
from app.core.messages import THANK_YOU_MESSAGE
from app.core.security import VerifyPassword
from app.repositories.user_repository import UserRepository
from app.schemas.auth import CurrentUserResponse, LoginRequest, LoginResponse, LogoutResponse
from app.services.auth import login_throttle

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CC00"
# The operations logged with a file condition: the USRSEC READ of READ-USER-SEC-FILE and the
# sign-on throttle's LOGINATT check-slot admission and failure-count writes and reset delete.
READ_OPERATION = "READ"
WRITE_OPERATION = "WRITE"
DELETE_OPERATION = "DELETE"

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


def EditUserIdPresent(request: LoginRequest) -> None:
    """Port the first WHEN of PROCESS-ENTER-KEY's EVALUATE TRUE (lines 117-122): a blank user ID.

    Raise the 401 failure of a USERID of SPACES OR LOW-VALUES. SignOn runs this edit before the
    throttle, so it is never counted or answered with 429. A blank user ID sets ERR-FLG-ON, which
    skips READ-USER-SEC-FILE (lines 138-140): it checks no password and names no user ID for the
    throttle, which is keyed by the upper-cased user ID. Counting it would give every blank
    sign-on one shared key that any client could lock.
    """
    if IsBlank(request.user_id):
        raise SignOnError(USER_ID_REQUIRED_MESSAGE, USER_ID_FIELD)


def EditCredentialPresent(request: LoginRequest) -> None:
    """Port the second WHEN of PROCESS-ENTER-KEY's EVALUATE TRUE (lines 123-127): a blank password.

    Raise the 401 failure of a PASSWD of SPACES OR LOW-VALUES. It runs only once the user ID edit
    has passed, inside the attempt's check slot, so CheckSignOn counts it once against the
    upper-cased user ID, and a locked user ID answers the throttle's 429 instead.
    """
    if IsBlank(request.password):
        raise SignOnError(CREDENTIAL_REQUIRED_MESSAGE, CREDENTIAL_FIELD)


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


async def AcquireSignOnSlot(context: OnlineContext, upperUserId: str) -> login_throttle.CheckSlot:
    """Wait for a check slot of the upper-cased user ID once the user ID edit has passed.

    The failures counted since the last reset plus the checks in flight never exceed
    LOGIN_MAX_ATTEMPTS, so concurrent attempts of one ID never check more passwords than that
    before the lock, and an attempt over the limit waits for the outcomes in flight instead of
    answering 429. A locked user ID answers the throttle's 429 and is not counted. A throttle
    that cannot admit the attempt, or admits none within its wait limit, answers the 503 WHEN
    OTHER literal and counts nothing, so the control never fails open.
    """
    try:
        return await login_throttle.AcquireCheckSlot(context, upperUserId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, WRITE_OPERATION, error)
        raise UnableToVerifyError() from error


async def CountFailure(context: OnlineContext, upperUserId: str) -> None:
    """Count the failure of this admitted attempt against the upper-cased user ID.

    The count commits in the throttle's own write unit before the failure answers. A throttle
    that cannot count it answers the 503 WHEN OTHER literal instead, so no 401 goes out
    uncounted and the control fails closed.
    """
    try:
        await login_throttle.RecordFailure(context, upperUserId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, WRITE_OPERATION, error)
        raise UnableToVerifyError() from error


async def ClearFailures(context: OnlineContext, upperUserId: str) -> None:
    """Delete the user ID's failure count after a successful sign-on.

    A throttle that cannot reset answers the 503 WHEN OTHER literal instead of signing on, and
    SignOn counts that 503 as the attempt's failure, so the control fails closed.
    """
    try:
        await login_throttle.ResetAttempts(context, upperUserId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, DELETE_OPERATION, error)
        raise UnableToVerifyError() from error


async def LookUpUser(context: OnlineContext, upperUserId: str) -> dict[str, Any] | None:
    """Port the CICS READ of USRSEC (lines 211-219): the record, or None for NOTFND (RESP 13).

    Any other condition, the batch window's NOTOPEN included, is the WHEN OTHER branch
    (lines 252-256): it answers the 503 literal, which SignOn counts as the attempt's failure.
    """
    try:
        return await UserRepository(context).GetUser(upperUserId)
    except RecordNotFoundError:
        return None
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        raise UnableToVerifyError() from error


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
    ``Wrong Password. Try again ...``; SignOn counts each as the attempt's failure.
    """
    userRow = await LookUpUser(context, upperUserId)
    if userRow is None:
        raise SignOnError(USER_NOT_FOUND_MESSAGE, USER_ID_FIELD)
    if not await CredentialMatches(upperCredential, userRow["password_hash"]):
        raise SignOnError(WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD)
    return userRow


async def CheckSignOn(
    context: OnlineContext, request: LoginRequest, upperUserId: str, upperCredential: str
) -> LoginResponse:
    """Run the password edit, the USRSEC read and compare, and the reset of one admitted attempt.

    The user ID edit has already passed. Every failure here, the blank password and the 503 of
    USRSEC or of the reset included, counts once against the upper-cased user ID before it is
    raised again: a 503 is not a sign-on.
    """
    try:
        EditCredentialPresent(request)
        userRow = await VerifyUser(context, upperUserId, upperCredential)
        await ClearFailures(context, upperUserId)
    except LegacyMessageError:
        await CountFailure(context, upperUserId)
        raise
    return BuildLoginResponse(userRow)


async def SignOn(context: OnlineContext, request: LoginRequest) -> LoginResponse:
    """Port PROCESS-ENTER-KEY (lines 108-140) and READ-USER-SEC-FILE (lines 209-257).

    The user ID edit runs first, before the throttle: a blank user ID names no key, so it holds
    no check slot and counts nothing. Any other attempt then waits for a check slot of the
    upper-cased ID, so the failures counted since the last reset plus the checks in flight never
    exceed LOGIN_MAX_ATTEMPTS. The admitted attempt then runs the password edit and the check; a
    failure counts once and a successful sign-on clears the count. The slot is released only
    after that outcome has committed.

    Raises:
        LegacyMessageError: 401 ``Please enter User ID ...`` for a blank user ID, answered
            before the throttle, counting nothing and never refused with 429; 429 with the
            lockout literal while the user ID is locked, counting nothing; 503 with
            ``Unable to verify the User ...`` when USRSEC or the throttle fails, the batch
            window and the throttle's wait limit included; 401 with the COSGN00C literal and
            its cursor field for every other sign-on failure.
    """
    EditUserIdPresent(request)
    upperUserId = NormaliseSignOnInput(request.user_id)
    upperCredential = NormaliseSignOnInput(request.password)
    checkSlot = await AcquireSignOnSlot(context, upperUserId)
    try:
        return await CheckSignOn(context, request, upperUserId, upperCredential)
    finally:
        await login_throttle.ReleaseCheckSlot(checkSlot)


def SignOff() -> LogoutResponse:
    """Port the PF3 branch of MAIN-PARA (lines 88-90): the CSMSG01Y CCDA-MSG-THANK-YOU literal."""
    return LogoutResponse(message=THANK_YOU_MESSAGE)


async def ReadDisplayNames(context: OnlineContext, userId: str) -> DisplayNames:
    """Return the user's names for the screen header, or blank names when USRSEC cannot say.

    The names are display only. A deleted user keeps a valid session until the next sign-on,
    and the menus touch no batch-window file, so NOTFND and every other file condition, NOTOPEN
    included, give blank names rather than an error; a file condition is still logged.
    """
    try:
        userRow = await UserRepository(context).GetUser(userId)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
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
