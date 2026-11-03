"""Replaces: COUSR01C (CU01), BMS COUSR01, copybooks CSUSR01Y.

The admin add-user screen. ENTER (PROCESS-ENTER-KEY) checks the five fields for presence in
source order, and the first blank one ends validation. It then writes the USRSEC record
(WRITE-USER-SEC-FILE) with the values as keyed: nothing is upper-cased and the user type is
not restricted, so any non-blank character is stored. Only exactly ``A`` later signs on as an
admin. The password is stored as a bcrypt hash of the text as entered, case kept. Sign-on
upper-cases its input (COSGN00C), so a password created in lower case never signs on; that is
the legacy outcome and is kept.

Legacy paths with no code here: ``EIBCALEN = 0`` (back to sign-on) is the admin guard and its
401; PF3 (``XCTL COADM01C``) and PF4 (CLEAR-CURRENT-SCREEN) are frontend navigation and form
clearing; POPULATE-HEADER-INFO is the frontend ScreenShell. COUSR01's legend shows F12=Exit,
but the program has no PF12 branch, so F12 reaches the invalid-key path, RejectInvalidKey, with
the CSMSG01Y CCDA-MSG-INVALID-KEY literal of core.messages.
"""

import asyncio
import logging
from typing import NamedTuple, NoReturn

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_RECORD_CHANGED,
    DuplicateKeyError,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.messages import INVALID_KEY_MESSAGE
from app.core.security import HashPassword
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.schemas.users import UserCreateRequest, UserMessageResponse

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CU01"
# The CICS command of WRITE-USER-SEC-FILE, as its logged file conditions name it.
WRITE_OPERATION = "WRITE"


class RequiredField(NamedTuple):
    """One presence check of PROCESS-ENTER-KEY: the request field, its cursor and its literal.

    ``cursorField`` is the JSON field name that replaces ``MOVE -1 TO <field>L``.
    """

    attributeName: str
    cursorField: str
    message: str


# PROCESS-ENTER-KEY lines 117-147, in source order: the first blank field ends validation.
REQUIRED_FIELDS = (
    RequiredField("first_name", "first_name", "First Name can NOT be empty..."),
    RequiredField("last_name", "last_name", "Last Name can NOT be empty..."),
    RequiredField("user_id", "user_id", "User ID can NOT be empty..."),
    RequiredField("password", "password", "Password can NOT be empty..."),
    RequiredField("user_type", "user_type", "User Type can NOT be empty..."),
)

# WRITE-USER-SEC-FILE lines 260-273: DUPKEY/DUPREC (cursor USERIDL), then WHEN OTHER (FNAMEL).
USER_ID_EXISTS_MESSAGE = "User ID already exist..."
UNABLE_TO_ADD_USER_MESSAGE = "Unable to Add User..."
USER_ID_FIELD = "user_id"
# INITIALIZE-ALL-FIELDS (MOVE -1 TO FNAMEL), the WHEN OTHER write failure and the invalid-key
# path all put the cursor on the first-name field.
FIRST_NAME_FIELD = "first_name"
# 409 Conflict: the key exists already. errors.py names the status after its other literal,
# "Record changed by some one else. Please review".
USER_ID_EXISTS_STATUS = HTTP_RECORD_CHANGED

# The STRING of lines 255-258; SEC-USR-ID is DELIMITED BY SPACE, so only the characters before
# its first space are shown.
USER_ADDED_TEMPLATE = "User {userId} has been added ..."
SUCCESS_CURSOR_FIELD = FIRST_NAME_FIELD

# The padding a BMS input field carries; the USRSEC columns hold the text right-trimmed.
FIELD_PADDING = " "


def IsBlank(value: str) -> bool:
    """Return True for ``SPACES OR LOW-VALUES``: an empty field or one holding only spaces."""
    return not value.strip(FIELD_PADDING)


def TrimFieldPadding(value: str) -> str:
    """Return the keyed text without its trailing BMS padding; leading spaces are kept."""
    return value.rstrip(FIELD_PADDING)


def CheckRequiredFields(request: UserCreateRequest) -> None:
    """Port the EVALUATE TRUE of PROCESS-ENTER-KEY (lines 117-151).

    Raises:
        LegacyMessageError: 400 with the first blank field's literal and that field as the
            cursor. No other edit exists: format, case and user-type values are not checked.
    """
    for requiredField in REQUIRED_FIELDS:
        if IsBlank(getattr(request, requiredField.attributeName)):
            raise LegacyMessageError(
                requiredField.message,
                field=requiredField.cursorField,
                httpStatus=HTTP_EDIT_FAILURE,
            )


def BuildUserValues(request: UserCreateRequest, passwordHash: str) -> dict[str, str]:
    """Return the SEC-USER-DATA record of lines 154-158, keyed by USRSEC column name.

    Each field is moved as keyed, never upper-cased; ``passwordHash`` replaces SEC-USR-PWD.
    """
    return {
        "user_id": TrimFieldPadding(request.user_id),
        "first_name": TrimFieldPadding(request.first_name),
        "last_name": TrimFieldPadding(request.last_name),
        "password_hash": passwordHash,
        "user_type": TrimFieldPadding(request.user_type),
    }


async def WriteUserSecFile(context: OnlineContext, userValues: dict[str, str]) -> None:
    """Port WRITE-USER-SEC-FILE (lines 238-274): one write unit that commits on exit.

    The ``try`` wraps the whole unit, so a commit-time failure maps like a write failure.
    RepositoryError also covers core.locks.FileNotOpenError, the NOTOPEN of the batch window,
    which takes the WHEN OTHER branch as CLOSEFIL's closed USRSEC did.

    Raises:
        LegacyMessageError: 409 ``User ID already exist...`` for DUPKEY/DUPREC, or 503
            ``Unable to Add User...`` for any other file condition.
    """
    try:
        async with WriteUnit(context.engine, USER_FILE_NAME) as unit:
            await UserRepository(context).InsertUser(unit, userValues)
    except DuplicateKeyError as error:
        raise LegacyMessageError(
            USER_ID_EXISTS_MESSAGE, field=USER_ID_FIELD, httpStatus=USER_ID_EXISTS_STATUS
        ) from error
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, WRITE_OPERATION, error)
        raise LegacyMessageError(
            UNABLE_TO_ADD_USER_MESSAGE, field=FIRST_NAME_FIELD, httpStatus=HTTP_FILE_ERROR
        ) from error


def AddedMessage(userId: str) -> str:
    """Return ``User <id> has been added ...`` with the ID up to its first space (line 256)."""
    return USER_ADDED_TEMPLATE.format(userId=userId.partition(FIELD_PADDING)[0])


async def AddUser(context: OnlineContext, request: UserCreateRequest) -> UserMessageResponse:
    """Port CU01 ENTER: PROCESS-ENTER-KEY, then WRITE-USER-SEC-FILE.

    The password is hashed as entered, case kept and padding removed, at
    ``context.settings.BCRYPT_ROUNDS``. Hashing runs in a worker thread before the write unit
    opens, so no pooled connection or event-loop turn waits on bcrypt. The response carries
    neither the password nor its hash; the page shows the message in green and clears the form.

    Raises:
        LegacyMessageError: from CheckRequiredFields (400) or WriteUserSecFile (409, 503).
    """
    CheckRequiredFields(request)
    passwordHash = await asyncio.to_thread(
        HashPassword, TrimFieldPadding(request.password), context.settings.BCRYPT_ROUNDS
    )
    userValues = BuildUserValues(request, passwordHash)
    await WriteUserSecFile(context, userValues)
    return UserMessageResponse(
        message=AddedMessage(userValues["user_id"]), cursor_field=SUCCESS_CURSOR_FIELD
    )


def RejectInvalidKey() -> NoReturn:
    """Port the ``WHEN OTHER`` AID branch of MAIN-PARA (lines 98-102), which F12 reaches.

    Raises:
        LegacyMessageError: always, 400 ``Invalid key pressed. Please see below...`` with the
            cursor on the first-name field. No database is touched.
    """
    raise LegacyMessageError(
        INVALID_KEY_MESSAGE, field=FIRST_NAME_FIELD, httpStatus=HTTP_EDIT_FAILURE
    )
