"""Replaces: COUSR03C (CU03), BMS COUSR03, copybooks CSUSR01Y.

The CardDemo delete-user screen of the admin menu. ENTER fetches the user (PROCESS-ENTER-KEY)
and F5 deletes it (DELETE-USER-INFO). Both end at the first failing edit, as the source's
``EVALUATE`` and ``ERR-FLG`` chain does, and every message is the COUSR03C literal verbatim.

The rest of MAIN-PARA has no code here. ``EIBCALEN = 0`` is the admin guard's 401; the first-entry
fetch of the user COUSR00C selected (lines 99-104) is the page calling FetchUserForDelete with its
``userId`` query parameter; F3, F4 and F12 are client navigation and form clearing; any other AID
key cannot be sent; and POPULATE-HEADER-INFO is the frontend ``ScreenShell``.

USRSEC access follows the record-locking design of the online port:

- The fetch's ``READ ... UPDATE`` is a plain read on the request session: its lock ended with the
  display task, so nothing is locked across the two screens.
- The delete is one write unit holding a single keyed DELETE whose row count is the NOTFND test,
  replacing the ``READ ... UPDATE`` and ``DELETE`` pair.

The user ID is used as keyed, never upper-cased. Deleting a user leaves that user's existing
session valid until it expires, as a CICS session kept its COMMAREA; an admin may delete their
own user, as in the source. No response carries a password or a password hash.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    LegacyMessageError,
    RepositoryError,
)
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.schemas.users import UserDetailResponse, UserMessageResponse

LOGGER = logging.getLogger(__name__)

# COUSR03C message literals, verbatim.
USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty..."
USER_NOT_FOUND_MESSAGE = "User ID NOT found..."
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
PRESS_PF5_TO_DELETE_MESSAGE = "Press PF5 key to delete this user ..."
# The source's delete-failure literal says "Update" (DELETE-USER-SEC-FILE line 332).
UNABLE_TO_UPDATE_USER_MESSAGE = "Unable to Update User..."
USER_DELETED_TEMPLATE = "User {userId} has been deleted ..."

# Cursor targets: MOVE -1 TO USRIDINL (the user ID) and MOVE -1 TO FNAMEL (the first name).
USER_ID_FIELD = "user_id"
FIRST_NAME_FIELD = "first_name"

# The WHEN OTHER ``DISPLAY 'RESP:' WS-RESP-CD 'REAS:' WS-REAS-CD`` of both file paragraphs.
FILE_ERROR_LOG = "RESP:%d REAS:%d"


def RequireUserId(userId: str) -> None:
    """Reject a blank user ID, the first edit of PROCESS-ENTER-KEY and DELETE-USER-INFO.

    A field the frontend sends is never LOW-VALUES (the screen-text repertoire excludes NUL),
    so ``USRIDINI = SPACES OR LOW-VALUES`` is a test for empty or all-space text.

    Raises:
        LegacyMessageError: 400 ``User ID can NOT be empty...``, cursor on the user ID.
    """
    if not userId.strip(" "):
        raise LegacyMessageError(
            USER_ID_EMPTY_MESSAGE, field=USER_ID_FIELD, httpStatus=HTTP_EDIT_FAILURE
        )


def FileErrorOutcome(error: RepositoryError, message: str) -> LegacyMessageError:
    """Return the WHEN OTHER outcome of a USRSEC command: 503, ``message``, cursor first name.

    The condition is logged as the source's RESP and REAS DISPLAY. The batch window's NOTOPEN
    (FileNotOpenError), a record lock (RecordLockError) and an I/O error all arrive here.
    """
    LOGGER.warning(FILE_ERROR_LOG, error.resp, error.resp2)
    return LegacyMessageError(message, field=FIRST_NAME_FIELD, httpStatus=HTTP_FILE_ERROR)


def ToDetailResponse(userRecord: Mapping[str, Any]) -> UserDetailResponse:
    """Move SEC-USR-FNAME, SEC-USR-LNAME and SEC-USR-TYPE to the map, with the PF5 prompt.

    The COUSR03 map has no password field, and the stored hash is never copied.
    """
    return UserDetailResponse(
        user_id=userRecord["user_id"],
        first_name=userRecord["first_name"],
        last_name=userRecord["last_name"],
        user_type=userRecord["user_type"],
        message=PRESS_PF5_TO_DELETE_MESSAGE,
    )


async def ReadUserSecFile(context: OnlineContext, userId: str) -> Mapping[str, Any]:
    """Port READ-USER-SEC-FILE (lines 267-300): read the USRSEC record of ``userId``.

    Raises:
        LegacyMessageError: 404 ``User ID NOT found...`` (NOTFND), cursor on the user ID;
            503 ``Unable to lookup User...`` (WHEN OTHER), the batch window included.
    """
    try:
        userRecord = await UserRepository(context).GetUser(userId)
    except RepositoryError as error:
        raise FileErrorOutcome(error, UNABLE_TO_LOOKUP_USER_MESSAGE) from error
    if userRecord is None:
        raise LegacyMessageError(
            USER_NOT_FOUND_MESSAGE, field=USER_ID_FIELD, httpStatus=HTTP_NOT_FOUND
        )
    return userRecord


async def FetchUserForDelete(context: OnlineContext, userId: str) -> UserDetailResponse:
    """Port PROCESS-ENTER-KEY (lines 142-169): show the user that F5 would delete.

    Args:
        context: The admin request's context.
        userId: The USRIDIN field as keyed, up to 8 characters.

    Returns:
        The user's ID, names and type, with ``Press PF5 key to delete this user ...``.

    Raises:
        LegacyMessageError: 400 for a blank user ID, 404 when the user does not exist, 503
            when USRSEC cannot be read.
    """
    RequireUserId(userId)
    userRecord = await ReadUserSecFile(context, userId)
    return ToDetailResponse(userRecord)


async def DeleteUserSecFile(context: OnlineContext, userId: str) -> int:
    """Port DELETE-USER-SEC-FILE (lines 305-336): delete ``userId`` in one write unit.

    Returns the number of records deleted, 0 being NOTFND. The unit commits when it exits.

    Raises:
        LegacyMessageError: 503 ``Unable to Update User...`` (WHEN OTHER) for any USRSEC
            condition, the batch window and a record lock included.
    """
    repository = UserRepository(context)
    try:
        async with WriteUnit(context.engine, USER_FILE_NAME) as unit:
            deletedCount = await repository.DeleteUser(unit, userId)
    except RepositoryError as error:
        raise FileErrorOutcome(error, UNABLE_TO_UPDATE_USER_MESSAGE) from error
    return deletedCount


async def DeleteUser(context: OnlineContext, userId: str) -> UserMessageResponse:
    """Port DELETE-USER-INFO (lines 174-192), the F5 key: delete the user.

    The keyed DELETE's row count replaces the READ UPDATE that preceded the DELETE, so an
    absent user gives ``User ID NOT found...`` with nothing written.

    Args:
        context: The admin request's context.
        userId: The USRIDIN field as keyed, up to 8 characters.

    Returns:
        ``User <id> has been deleted ...``, the ID taken up to its first space (STRING ...
        DELIMITED BY SPACE), with the cursor on the user ID that INITIALIZE-ALL-FIELDS clears.

    Raises:
        LegacyMessageError: 400 for a blank user ID, 404 when the user does not exist, 503
            when USRSEC cannot be updated.
    """
    RequireUserId(userId)
    deletedCount = await DeleteUserSecFile(context, userId)
    if deletedCount == 0:
        raise LegacyMessageError(
            USER_NOT_FOUND_MESSAGE, field=USER_ID_FIELD, httpStatus=HTTP_NOT_FOUND
        )
    deletedUserId = userId.partition(" ")[0]
    return UserMessageResponse(
        message=USER_DELETED_TEMPLATE.format(userId=deletedUserId), cursor_field=USER_ID_FIELD
    )
