"""Replaces: COUSR02C (CU02), BMS COUSR02, copybooks CSUSR01Y.

The admin update-user screen. ENTER (PROCESS-ENTER-KEY) fetches the user; F5 and F3
(UPDATE-USER-INFO) rewrite it. Validation stops at the first failing check, as the program's
``EVALUATE`` chains do, so every outcome carries one message and one cursor field.

The USRSEC password is a bcrypt hash, so the stored SEC-USR-PWD that COUSR02C showed on fetch
and compared on update no longer exists. The fetch therefore returns no password, the update
skips the password presence check (``Password can NOT be empty...`` is unreachable here), a
blank password keeps the stored hash, and a non-blank one replaces it only when it does not
verify against that hash.

Legacy paths with no code here: ``EIBCALEN = 0`` (back to sign-on) is the session guard's 401;
the first-entry fetch of the user COUSR00C selected (lines 99-104) is the frontend calling
FetchUser with its ``userId`` query parameter; F3 calls UpdateUser and then navigates back,
whatever the outcome (lines 111-119); F4 (CLEAR-CURRENT-SCREEN) and F12 (back to COADM01C
without saving) are client-side; other AID keys cannot reach the server; POPULATE-HEADER-INFO
is the frontend ScreenShell.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, NamedTuple

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RecordLockError,
    RecordNotFoundError,
    RepositoryError,
)
from app.core.locks import FileNotOpenError
from app.core.security import HashPassword, VerifyPassword
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.schemas.users import UserDetailResponse, UserMessageResponse, UserUpdateRequest

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CU02"
# The CICS commands logged with a file condition: the fetch's READ, and the update unit's
# READ UPDATE and REWRITE, whichever of the two failed.
READ_OPERATION = "READ"
UPDATE_OPERATION = "READ UPDATE/REWRITE"

# Message literals of COUSR02C, verbatim.
USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty..."
FIRST_NAME_EMPTY_MESSAGE = "First Name can NOT be empty..."
LAST_NAME_EMPTY_MESSAGE = "Last Name can NOT be empty..."
USER_TYPE_EMPTY_MESSAGE = "User Type can NOT be empty..."
USER_NOT_FOUND_MESSAGE = "User ID NOT found..."
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
PRESS_PF5_TO_SAVE_MESSAGE = "Press PF5 key to save your updates ..."
PLEASE_MODIFY_MESSAGE = "Please modify to update ..."
UNABLE_TO_UPDATE_USER_MESSAGE = "Unable to Update User..."
# UPDATE-USER-SEC-FILE: STRING 'User ' SEC-USR-ID DELIMITED BY SPACE ' has been updated ...'.
USER_UPDATED_TEMPLATE = "User {userId} has been updated ..."

# COUSR2AI fields, by the snake_case names of the request and response models.
USER_ID_FIELD = "user_id"
FIRST_NAME_FIELD = "first_name"
LAST_NAME_FIELD = "last_name"
USER_TYPE_FIELD = "user_type"
# The security.users column holding the bcrypt hash that replaces SEC-USR-PWD.
HASH_COLUMN = "password_hash"
# The space padding of a PIC X field: COBOL compares padded fields, so trailing spaces are
# never significant, while leading spaces and case are.
FIELD_PADDING = " "


class RequiredField(NamedTuple):
    """One presence check of UPDATE-USER-INFO: the input it tests, its cursor and its literal."""

    source: str
    cursorField: str
    message: str


# UPDATE-USER-INFO lines 179-213 in source order, without the PASSWDI check (lines 198-203):
# under hashing a fetch cannot show the password, so a blank one means "keep it".
REQUIRED_UPDATE_FIELDS: tuple[RequiredField, ...] = (
    RequiredField(USER_ID_FIELD, USER_ID_FIELD, USER_ID_EMPTY_MESSAGE),
    RequiredField(FIRST_NAME_FIELD, FIRST_NAME_FIELD, FIRST_NAME_EMPTY_MESSAGE),
    RequiredField(LAST_NAME_FIELD, LAST_NAME_FIELD, LAST_NAME_EMPTY_MESSAGE),
    RequiredField(USER_TYPE_FIELD, USER_TYPE_FIELD, USER_TYPE_EMPTY_MESSAGE),
)
# The fields lines 219-234 compare with the stored record, besides the password.
COMPARED_FIELDS = (FIRST_NAME_FIELD, LAST_NAME_FIELD, USER_TYPE_FIELD)


def IsBlank(value: str) -> bool:
    """Return True for SPACES OR LOW-VALUES: empty text or text of spaces only."""
    return not value.strip(FIELD_PADDING)


def WithoutPadding(value: Any) -> str:
    return str(value).rstrip(FIELD_PADDING)


def UserIdNotFound() -> LegacyMessageError:
    """Return the NOTFND outcome of READ-USER-SEC-FILE and UPDATE-USER-SEC-FILE, at USRIDIN."""
    return LegacyMessageError(
        USER_NOT_FOUND_MESSAGE, field=USER_ID_FIELD, httpStatus=HTTP_NOT_FOUND
    )


def ToDetailResponse(storedRecord: Mapping[str, Any]) -> UserDetailResponse:
    """Return the fetched user as COUSR02 shows it, never with a password or its hash."""
    return UserDetailResponse(
        user_id=WithoutPadding(storedRecord[USER_ID_FIELD]),
        first_name=WithoutPadding(storedRecord[FIRST_NAME_FIELD]),
        last_name=WithoutPadding(storedRecord[LAST_NAME_FIELD]),
        user_type=WithoutPadding(storedRecord[USER_TYPE_FIELD]),
        message=PRESS_PF5_TO_SAVE_MESSAGE,
    )


async def FetchUser(context: OnlineContext, userId: str) -> UserDetailResponse:
    """Port PROCESS-ENTER-KEY (lines 143-172) and its READ-USER-SEC-FILE (lines 320-353).

    The read locks nothing: COUSR02C's READ UPDATE held its lock only until the display task
    ended. The user ID is used as entered, never upper-cased.

    Raises:
        LegacyMessageError: 400 ``User ID can NOT be empty...`` for a blank user ID; 404
            ``User ID NOT found...`` for NOTFND; 503 ``Unable to lookup User...`` for the
            batch window (NOTOPEN) or any other file condition (``WHEN OTHER``).
    """
    if IsBlank(userId):
        raise LegacyMessageError(USER_ID_EMPTY_MESSAGE, field=USER_ID_FIELD)
    try:
        storedRecord = await UserRepository(context).GetUser(userId)
    except (FileNotOpenError, RepositoryError) as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        raise LegacyMessageError(
            UNABLE_TO_LOOKUP_USER_MESSAGE, field=FIRST_NAME_FIELD, httpStatus=HTTP_FILE_ERROR
        ) from error
    if storedRecord is None:
        raise UserIdNotFound()
    return ToDetailResponse(storedRecord)


def ScreenValues(userId: str, request: UserUpdateRequest) -> dict[str, str]:
    """Return the tested COUSR2AI inputs without trailing padding; the user ID is the path key."""
    return {
        USER_ID_FIELD: WithoutPadding(userId),
        FIRST_NAME_FIELD: WithoutPadding(request.first_name),
        LAST_NAME_FIELD: WithoutPadding(request.last_name),
        USER_TYPE_FIELD: WithoutPadding(request.user_type),
    }


def CheckRequiredFields(screenValues: Mapping[str, str]) -> None:
    """Raise the first presence failure of REQUIRED_UPDATE_FIELDS, with 400 and its cursor."""
    for requiredField in REQUIRED_UPDATE_FIELDS:
        if IsBlank(screenValues[requiredField.source]):
            raise LegacyMessageError(requiredField.message, field=requiredField.cursorField)


async def ResolvePasswordHash(enteredPassword: str, storedHash: str, rounds: int) -> str:
    """Return the hash to rewrite: the stored one, or a new hash of a changed password.

    A blank password keeps the stored hash. A non-blank one is a change only when it does not
    verify against the stored hash (the ``PASSWDI NOT = SEC-USR-PWD`` test); the new hash is of
    the text as entered, case kept, as COUSR02C moves PASSWDI into SEC-USR-PWD. bcrypt runs in
    a worker thread so the event loop keeps serving other requests.
    """
    if not enteredPassword:
        return storedHash
    if await asyncio.to_thread(VerifyPassword, enteredPassword, storedHash):
        return storedHash
    return await asyncio.to_thread(HashPassword, enteredPassword, rounds)


def IsModified(
    screenValues: Mapping[str, str], storedRecord: Mapping[str, Any], passwordHash: str
) -> bool:
    """Return True when any compared field differs from the stored record (USR-MODIFIED-YES)."""
    if passwordHash != storedRecord[HASH_COLUMN]:
        return True
    return any(
        screenValues[fieldName] != WithoutPadding(storedRecord[fieldName])
        for fieldName in COMPARED_FIELDS
    )


def RewriteValues(
    screenValues: Mapping[str, str], storedRecord: Mapping[str, Any], passwordHash: str
) -> dict[str, str]:
    """Return the whole record UPDATE-USER-SEC-FILE rewrites: key, names, hash and user type."""
    rewriteValues = {fieldName: screenValues[fieldName] for fieldName in COMPARED_FIELDS}
    rewriteValues[USER_ID_FIELD] = storedRecord[USER_ID_FIELD]
    rewriteValues[HASH_COLUMN] = passwordHash
    return rewriteValues


async def RewriteUserRecord(
    context: OnlineContext, screenValues: Mapping[str, str], enteredPassword: str
) -> None:
    """Port the READ UPDATE and REWRITE of UPDATE-USER-INFO (lines 215-243) in one write unit.

    The record stays locked from the read to the unit's commit. There is no before-image
    compare: the last writer wins, as in COUSR02C. A NOTFND read answers ``User ID NOT
    found...`` at once rather than falling through to a REWRITE that has no record to replace.
    An outcome raised inside the unit rolls it back, which discards nothing, since nothing has
    been written by then. ``Please modify to update ...`` sets no ERR-FLG and the cursor is the
    one the presence checks' WHEN OTHER moved to FNAME (lines 208-210), so it lists no field in
    ``field_errors``.
    """
    repository = UserRepository(context)
    async with WriteUnit(context.engine, USER_FILE_NAME) as unit:
        storedRecord = await repository.GetUserForUpdate(unit, screenValues[USER_ID_FIELD])
        if storedRecord is None:
            raise UserIdNotFound()
        rounds = context.settings.BCRYPT_ROUNDS
        passwordHash = await ResolvePasswordHash(enteredPassword, storedRecord[HASH_COLUMN], rounds)
        if not IsModified(screenValues, storedRecord, passwordHash):
            raise LegacyMessageError(
                PLEASE_MODIFY_MESSAGE,
                field=FIRST_NAME_FIELD,
                details=LegacyErrorDetails(exactFieldErrors=True),
            )
        await repository.UpdateUser(unit, RewriteValues(screenValues, storedRecord, passwordHash))


def UpdatedMessage(userId: str) -> str:
    """Return ``User <id> has been updated ...``, the ID cut at its first space."""
    return USER_UPDATED_TEMPLATE.format(userId=userId.partition(FIELD_PADDING)[0])


async def UpdateUser(
    context: OnlineContext, userId: str, request: UserUpdateRequest
) -> UserMessageResponse:
    """Port UPDATE-USER-INFO (lines 177-245) and UPDATE-USER-SEC-FILE (lines 358-390): F5 and F3.

    Inputs lose only their trailing space padding; leading spaces and case are kept.

    Raises:
        LegacyMessageError: 400 with the first presence literal in source order (user ID,
            first name, last name, user type); 400 ``Please modify to update ...`` when no
            field changed, with an empty ``field_errors`` and the cursor on the first name;
            404 ``User ID NOT found...`` for NOTFND; 503
            ``Unable to Update User...`` for the batch window (NOTOPEN), a record lock held past
            the unit's lock timeout, or any other file condition.
    """
    screenValues = ScreenValues(userId, request)
    CheckRequiredFields(screenValues)
    try:
        await RewriteUserRecord(context, screenValues, WithoutPadding(request.password))
    except RecordNotFoundError as error:
        raise UserIdNotFound() from error
    # A failed READ UPDATE, the batch window's included, answers with the REWRITE's WHEN OTHER
    # literal: COUSR02C runs on past a failed READ, and the REWRITE that follows, with no
    # record held for update, fails and sends the last message the screen shows.
    except (FileNotOpenError, RecordLockError, RepositoryError) as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, UPDATE_OPERATION, error)
        raise LegacyMessageError(
            UNABLE_TO_UPDATE_USER_MESSAGE, field=FIRST_NAME_FIELD, httpStatus=HTTP_FILE_ERROR
        ) from error
    return UserMessageResponse(message=UpdatedMessage(userId), cursor_field=FIRST_NAME_FIELD)
