"""Unit tests for COUSR02C (CU02), the update-user screen of map COUSR02, under password hashing.

cbl/COUSR02C.cbl PROCESS-ENTER-KEY (lines 143-172) is the ENTER fetch: a blank USRIDIN answers
``User ID can NOT be empty...`` (lines 146-151), and READ-USER-SEC-FILE (lines 320-353) answers
``Press PF5 key to save your updates ...``, NOTFND ``User ID NOT found...`` and any other
condition ``Unable to lookup User...`` with the cursor on FNAME. UPDATE-USER-INFO (lines 177-245)
checks presence in source order, compares each field with the stored record (lines 219-234) and
either rewrites it or answers ``Please modify to update ...``. UPDATE-USER-SEC-FILE (lines
358-390) answers ``User <id> has been updated ...``, NOTFND ``User ID NOT found...`` and any
other condition ``Unable to Update User...``. F5 (lines 122-123) and F3 (lines 111-119, save
then return to the caller) both perform UPDATE-USER-INFO, so the router calls
cousr02c.UpdateUser for both; F12 returns without saving on the client alone.

SEC-USR-PWD is a bcrypt hash in security.users, so the fetch shows no password, the password
presence check of lines 198-203 is skipped, a blank password keeps the stored hash, and a typed
one replaces it, hashed as entered, only when it does not verify against that hash. No response
or error body carries a password or a hash.

Expected values are typed from those lines. Each test calls the services directly with an admin
context on a generated user of type U, against the migrated, seeded test database, and reads
the security.users row back through syncEngine; restoreSeedState restores every table, the ten
seed users and security.login_attempts included, at teardown.
"""

import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import datetime
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text

from app.core.errors import (
    FileIoError,
    LegacyMessageError,
    RecordLockError,
    RecordNotFoundError,
    RepositoryError,
)
from app.core.locks import FileNotOpenError
from app.core.security import VerifyPassword
from app.repositories.user_repository import UserRepository
from app.schemas.auth import LoginRequest, LoginResponse
from app.schemas.users import UserUpdateRequest
from app.services.auth import cosgn00c
from app.services.users import cousr02c

# COADM01C option 3 opens CU02 for an admin; the updated user is a generated regular user.
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# The CICS file name every stubbed file condition is raised against.
USRSEC_FILE_NAME = "USRSEC"
BLANK = ""
FIELD_PADDING = " "
# A SEC-USR-ID X(08) that neither the seed (ADMIN001-005, USER0001-0005) nor createUser makes.
UNKNOWN_USER_ID = "NOSUCH01"
# SPACES in the USRIDIN field of LENGTH 8, and spaces typed after a FNAME value.
SPACES_USER_ID = FIELD_PADDING * 8
TRAILING_PADDING = FIELD_PADDING * 3
# secrets.token_hex(4) is 8 hex digits: a PASSWD of LENGTH 8 that COSGN00C signs on unchanged.
REPLACEMENT_PASSWORD_BYTES = 4

# WS-MESSAGE literals of COUSR02C (lines 148, 188, 194, 206, 336, 342, 349, 239, 386) and the
# STRING of lines 372-375; COSGN00C's wrong-password literal (line 249).
USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty..."
FIRST_NAME_EMPTY_MESSAGE = "First Name can NOT be empty..."
LAST_NAME_EMPTY_MESSAGE = "Last Name can NOT be empty..."
USER_TYPE_EMPTY_MESSAGE = "User Type can NOT be empty..."
PRESS_PF5_TO_SAVE_MESSAGE = "Press PF5 key to save your updates ..."
USER_NOT_FOUND_MESSAGE = "User ID NOT found..."
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
PLEASE_MODIFY_MESSAGE = "Please modify to update ..."
UNABLE_TO_UPDATE_USER_MESSAGE = "Unable to Update User..."
USER_UPDATED_TEMPLATE = "User {userId} has been updated ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."

# The cursor fields (MOVE -1 TO USRIDINL, FNAMEL, LNAMEL, USRTYPEL; COSGN00C PASSWDL) and the
# request and response keys, by their snake_case names.
USER_ID_FIELD = "user_id"
FIRST_NAME_FIELD = "first_name"
LAST_NAME_FIELD = "last_name"
USER_TYPE_FIELD = "user_type"
CREDENTIAL_FIELD = "password"
MESSAGE_KEY = "message"
CURSOR_KEY = "cursor_field"
FIELD_ERRORS_KEY = "field_errors"
MENU_ROUTE = "/menu"

EDIT_FAILURE_STATUS = 400
SIGN_ON_FAILURE_STATUS = 401
NOT_FOUND_STATUS = 404
FILE_ERROR_STATUS = 503

CHANGED_FIRST_NAME = "Renamed"
CHANGED_LAST_NAME = "Resaved"
CHANGED_USER_TYPE = "A"

# A bcrypt hash reads $2b$<cost>$<salt and digest>: the cost is its third $-separated part.
HASH_SEPARATOR = "$"
HASH_COST_INDEX = 2

USER_ROW_QUERY = text(
    "SELECT first_name, last_name, user_type, password_hash, updated_at"
    " FROM security.users WHERE user_id = :userId"
)
# Every security.users row and column, the hash and both audit timestamps included, with xmin,
# the row version any UPDATE replaces even when it rewrites identical values.
ALL_USERS_QUERY = text(
    "SELECT CAST(xmin AS TEXT) AS row_version, * FROM security.users ORDER BY user_id"
)


class StoredUser(NamedTuple):
    """The security.users columns COUSR02C reads and rewrites, and the audit timestamp."""

    firstName: str
    lastName: str
    userType: str
    passwordHash: str
    updatedAt: datetime


class UpdateHarness(NamedTuple):
    """An admin's CU02 context, the generated user it updates, the engine and monkeypatch.

    ``context`` is the service-level OnlineContext; ``user`` is createUser's SignOnUser.
    """

    context: Any
    user: Any
    syncEngine: Engine
    monkeypatch: pytest.MonkeyPatch


class FetchRefusal(NamedTuple):
    """A USRIDIN derived from the created user's ID, and the outcome its fetch answers."""

    userIdOf: Callable[[str], str]
    expectedOutcome: tuple[str, str, int]


class FieldChange(NamedTuple):
    """One compared field typed with a new value, and the StoredUser attribute it rewrites."""

    fieldName: str
    newValue: str
    storedAttribute: str


class PresenceRefusal(NamedTuple):
    """The inputs left blank, and the first presence literal and cursor, in source order."""

    blankFields: tuple[str, ...]
    expectedMessage: str
    cursorField: str


class UpdateFailure(NamedTuple):
    """The UserRepository method that raises, and the CICS file condition it raises."""

    methodName: str
    errorType: type[RepositoryError]


class OutcomeBody(NamedTuple):
    """The JSON body an update outcome sends, and the password typed into PASSWD for it."""

    body: Mapping[str, Any]
    typedPassword: str


def ReadUser(syncEngine: Engine, userId: str) -> StoredUser:
    """Return the committed security.users row of ``userId``."""
    with syncEngine.connect() as connection:
        userRow = connection.execute(USER_ROW_QUERY, {"userId": userId}).one()
    return StoredUser(*userRow)


def StoredUserOf(harness: UpdateHarness) -> StoredUser:
    """Return the committed row of the harness's generated user."""
    return ReadUser(harness.syncEngine, harness.user.userId)


def ReadAllUsers(syncEngine: Engine) -> list[dict[str, object]]:
    """Return every committed security.users row, each column and its row version, in key order."""
    with syncEngine.connect() as connection:
        return [dict(userRow) for userRow in connection.execute(ALL_USERS_QUERY).mappings()]


@contextlib.contextmanager
def NoUserWrites(syncEngine: Engine) -> Iterator[None]:
    """Assert that the block inserts, rewrites and deletes no committed security.users row."""
    usersBefore = ReadAllUsers(syncEngine)
    yield
    assert ReadAllUsers(syncEngine) == usersBefore


def BuildUpdate(storedUser: StoredUser, overrides: Mapping[str, str]) -> UserUpdateRequest:
    """Return the COUSR02 inputs as fetched, a blank PASSWD, then ``overrides`` typed over them."""
    screenFields = {
        FIRST_NAME_FIELD: storedUser.firstName,
        LAST_NAME_FIELD: storedUser.lastName,
        CREDENTIAL_FIELD: BLANK,
        USER_TYPE_FIELD: storedUser.userType,
    }
    screenFields.update(overrides)
    return UserUpdateRequest(**screenFields)


async def SignOnWith(context: Any, userId: str, password: str) -> LoginResponse:
    """Sign ``userId`` on through COSGN00C, as ENTER on the COSGN00 screen does."""
    return await cosgn00c.SignOn(context, LoginRequest(user_id=userId, password=password))


def NewPassword() -> str:
    """Return a generated upper-case 8-character password no user has."""
    return secrets.token_hex(REPLACEMENT_PASSWORD_BYTES).upper()


def UpdatedMessage(userId: str) -> str:
    """Return the UPDATE-USER-SEC-FILE success literal for ``userId``."""
    return USER_UPDATED_TEMPLATE.format(userId=userId)


def HashCost(passwordHash: str) -> int:
    """Return the bcrypt cost a stored hash was made with."""
    return int(passwordHash.split(HASH_SEPARATOR)[HASH_COST_INDEX])


def ScreenOutcome(error: LegacyMessageError) -> tuple[str, str, int]:
    """Return the error body's message and cursor field, and the error's HTTP status."""
    errorBody = error.Body()
    return errorBody[MESSAGE_KEY], errorBody[CURSOR_KEY], error.httpStatus


async def FetchExpectingError(context: Any, userId: str) -> LegacyMessageError:
    """Press ENTER on COUSR02 with ``userId`` and return the legacy error it answers."""
    with pytest.raises(LegacyMessageError) as raised:
        await cousr02c.FetchUser(context, userId)
    return raised.value


async def UpdateExpectingError(
    context: Any, userId: str, request: UserUpdateRequest
) -> LegacyMessageError:
    """Press F5 on COUSR02 with ``request`` and return the legacy error it answers."""
    with pytest.raises(LegacyMessageError) as raised:
        await cousr02c.UpdateUser(context, userId, request)
    return raised.value


def RaiseOnRepository(
    harness: UpdateHarness, methodName: str, errorType: type[RepositoryError]
) -> None:
    """Make UserRepository.methodName raise ``errorType`` on USRSEC before any statement."""
    failure = AsyncMock(side_effect=errorType(USRSEC_FILE_NAME))
    harness.monkeypatch.setattr(UserRepository, methodName, failure)


async def ReplacePassword(harness: UpdateHarness) -> str:
    """Rewrite the generated user with only a new PASSWD typed; return that password."""
    newPassword = NewPassword()
    request = BuildUpdate(StoredUserOf(harness), {CREDENTIAL_FIELD: newPassword})
    response = await cousr02c.UpdateUser(harness.context, harness.user.userId, request)
    assert response.message == UpdatedMessage(harness.user.userId)
    return newPassword


async def RefusedUpdateBody(harness: UpdateHarness, overrides: Mapping[str, str]) -> OutcomeBody:
    """Update with ``overrides`` typed, expecting a refusal; return its body and PASSWD."""
    request = BuildUpdate(StoredUserOf(harness), overrides)
    error = await UpdateExpectingError(harness.context, harness.user.userId, request)
    return OutcomeBody(error.Body(), request.password)


async def ReplacedPasswordBody(harness: UpdateHarness) -> OutcomeBody:
    """Update with a new PASSWD typed; return the success body and that password."""
    newPassword = NewPassword()
    request = BuildUpdate(StoredUserOf(harness), {CREDENTIAL_FIELD: newPassword})
    response = await cousr02c.UpdateUser(harness.context, harness.user.userId, request)
    return OutcomeBody(response.model_dump(), newPassword)


async def ReEnteredPasswordBody(harness: UpdateHarness) -> OutcomeBody:
    """Re-enter the current password with nothing changed: the no-change refusal's body."""
    return await RefusedUpdateBody(harness, {CREDENTIAL_FIELD: harness.user.password})


async def PresenceRefusalBody(harness: UpdateHarness) -> OutcomeBody:
    """Blank FNAME with a new PASSWD typed: the first-name presence refusal's body."""
    overrides = {FIRST_NAME_FIELD: BLANK, CREDENTIAL_FIELD: NewPassword()}
    return await RefusedUpdateBody(harness, overrides)


async def FailedRewriteBody(harness: UpdateHarness) -> OutcomeBody:
    """A new PASSWD typed and the REWRITE failing with IOERR: the 503 refusal's body."""
    RaiseOnRepository(harness, "UpdateUser", FileIoError)
    return await RefusedUpdateBody(harness, {CREDENTIAL_FIELD: NewPassword()})


FETCH_REFUSALS = [
    FetchRefusal(lambda userId: BLANK, (USER_ID_EMPTY_MESSAGE, USER_ID_FIELD, EDIT_FAILURE_STATUS)),
    FetchRefusal(
        lambda userId: SPACES_USER_ID,
        (USER_ID_EMPTY_MESSAGE, USER_ID_FIELD, EDIT_FAILURE_STATUS),
    ),
    FetchRefusal(
        lambda userId: UNKNOWN_USER_ID, (USER_NOT_FOUND_MESSAGE, USER_ID_FIELD, NOT_FOUND_STATUS)
    ),
    # COUSR02C moves USRIDIN to SEC-USR-ID as keyed: unlike COSGN00C it never upper-cases it.
    FetchRefusal(str.lower, (USER_NOT_FOUND_MESSAGE, USER_ID_FIELD, NOT_FOUND_STATUS)),
]
FETCH_REFUSAL_IDS = ["blank-id", "spaces-id", "unknown-id", "lower-cased-id"]

# READ-USER-SEC-FILE WHEN OTHER (lines 346-352): the batch window's NOTOPEN and an IOERR.
FETCH_FILE_CONDITIONS = [FileNotOpenError, FileIoError]
FETCH_FILE_CONDITION_IDS = ["batch-window-notopen", "ioerr"]

SINGLE_FIELD_CHANGES = [
    FieldChange(FIRST_NAME_FIELD, CHANGED_FIRST_NAME, "firstName"),
    FieldChange(LAST_NAME_FIELD, CHANGED_LAST_NAME, "lastName"),
    FieldChange(USER_TYPE_FIELD, CHANGED_USER_TYPE, "userType"),
]
SINGLE_FIELD_CHANGE_IDS = ["first-name", "last-name", "user-type"]

# Lines 219-234 compare padded PIC X fields, so trailing spaces typed after a value change
# nothing; a re-entered current password verifies against the stored hash.
NO_CHANGE_OVERRIDES: list[Callable[[UpdateHarness, StoredUser], dict[str, str]]] = [
    lambda harness, storedUser: {},
    lambda harness, storedUser: {CREDENTIAL_FIELD: harness.user.password},
    lambda harness, storedUser: {FIRST_NAME_FIELD: storedUser.firstName + TRAILING_PADDING},
]
NO_CHANGE_OVERRIDE_IDS = ["blank-password", "current-password-re-entered", "trailing-padding"]

# Each case blanks the checked field and every later one, so the first literal in source order
# answers. PASSWD is blank in every case and is never checked: the user-type case passes the
# skipped password check of lines 198-203.
PRESENCE_REFUSALS = [
    PresenceRefusal(
        (USER_ID_FIELD, FIRST_NAME_FIELD, LAST_NAME_FIELD, USER_TYPE_FIELD),
        USER_ID_EMPTY_MESSAGE,
        USER_ID_FIELD,
    ),
    PresenceRefusal(
        (FIRST_NAME_FIELD, LAST_NAME_FIELD, USER_TYPE_FIELD),
        FIRST_NAME_EMPTY_MESSAGE,
        FIRST_NAME_FIELD,
    ),
    PresenceRefusal((LAST_NAME_FIELD, USER_TYPE_FIELD), LAST_NAME_EMPTY_MESSAGE, LAST_NAME_FIELD),
    PresenceRefusal((USER_TYPE_FIELD,), USER_TYPE_EMPTY_MESSAGE, USER_TYPE_FIELD),
]
PRESENCE_REFUSAL_IDS = ["user-id", "first-name", "last-name", "user-type"]

# The READ UPDATE locked past the unit's lock timeout (RECORDBUSY) or refused by the batch
# window (NOTOPEN), and the REWRITE failing (IOERR): UPDATE-USER-SEC-FILE WHEN OTHER (line 386).
UPDATE_FAILURES = [
    UpdateFailure("GetUserForUpdate", RecordLockError),
    UpdateFailure("GetUserForUpdate", FileNotOpenError),
    UpdateFailure("UpdateUser", FileIoError),
]
UPDATE_FAILURE_IDS = ["read-update-recordbusy", "read-update-batch-window", "rewrite-ioerr"]

OUTCOME_BODIES = [
    ReplacedPasswordBody,
    ReEnteredPasswordBody,
    PresenceRefusalBody,
    FailedRewriteBody,
]
OUTCOME_BODY_IDS = ["updated", "please-modify", "presence-refusal", "unable-to-update"]


@pytest_asyncio.fixture(name="harness")
async def UpdateHarnessFixture(
    makeOnlineContext: Callable[[str], Awaitable[Any]],
    createUser: Callable[[str], Any],
    syncEngine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> UpdateHarness:
    """Return an admin's context and a generated user of type U; restoreSeedState undoes both.

    createUser and makeOnlineContext depend on restoreSeedState, so every row a test commits is
    undone at teardown, after the context has released its batch-window lock.
    """
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    user = createUser(REGULAR_USER_TYPE)
    return UpdateHarness(context, user, syncEngine, monkeypatch)


# --- ENTER: PROCESS-ENTER-KEY and READ-USER-SEC-FILE --------------------------------------------
async def test_fetch_shows_the_stored_user_without_a_password_or_hash(harness):
    """NORMAL shows SEC-USR-ID, the names and the type: the body is exactly those and the prompt.

    The exact body leaves no room for a password or hash key, or a value equal to either, and
    the fetch locks and writes nothing.
    """
    storedUser = StoredUserOf(harness)
    response = await cousr02c.FetchUser(harness.context, harness.user.userId)
    assert response.model_dump() == {
        USER_ID_FIELD: harness.user.userId,
        FIRST_NAME_FIELD: storedUser.firstName,
        LAST_NAME_FIELD: storedUser.lastName,
        USER_TYPE_FIELD: storedUser.userType,
        MESSAGE_KEY: PRESS_PF5_TO_SAVE_MESSAGE,
    }
    assert StoredUserOf(harness) == storedUser


@pytest.mark.parametrize("refusal", FETCH_REFUSALS, ids=FETCH_REFUSAL_IDS)
async def test_fetch_refuses_a_blank_or_unknown_user_id(harness, refusal):
    """A blank USRIDIN (lines 146-151) is 400; NOTFND (lines 340-345) is 404, both at USRIDIN.

    The refused fetch inserts, rewrites and deletes no security.users row.
    """
    userId = refusal.userIdOf(harness.user.userId)
    with NoUserWrites(harness.syncEngine):
        error = await FetchExpectingError(harness.context, userId)
    assert ScreenOutcome(error) == refusal.expectedOutcome


@pytest.mark.parametrize("errorType", FETCH_FILE_CONDITIONS, ids=FETCH_FILE_CONDITION_IDS)
async def test_fetch_file_condition_is_unable_to_lookup(harness, errorType):
    """Any other READ condition is 503 ``Unable to lookup User...`` at FNAME (lines 346-352).

    The refused fetch inserts, rewrites and deletes no security.users row.
    """
    RaiseOnRepository(harness, "GetUser", errorType)
    with NoUserWrites(harness.syncEngine):
        error = await FetchExpectingError(harness.context, harness.user.userId)
    expectedOutcome = (UNABLE_TO_LOOKUP_USER_MESSAGE, FIRST_NAME_FIELD, FILE_ERROR_STATUS)
    assert ScreenOutcome(error) == expectedOutcome


# --- F5 and F3: UPDATE-USER-INFO and UPDATE-USER-SEC-FILE ---------------------------------------
@pytest.mark.parametrize("change", SINGLE_FIELD_CHANGES, ids=SINGLE_FIELD_CHANGE_IDS)
async def test_one_changed_field_rewrites_the_record_and_keeps_the_hash(harness, change):
    """One differing field sets USR-MODIFIED-YES; the blank PASSWD keeps the hash byte for byte.

    The blank PASSWD also passes, unchecked, the password presence check of lines 198-203.
    """
    before = StoredUserOf(harness)
    request = BuildUpdate(before, {change.fieldName: change.newValue})
    response = await cousr02c.UpdateUser(harness.context, harness.user.userId, request)
    assert response.model_dump() == {
        MESSAGE_KEY: UpdatedMessage(harness.user.userId),
        CURSOR_KEY: FIRST_NAME_FIELD,
    }
    after = StoredUserOf(harness)
    assert after.updatedAt > before.updatedAt
    rewritten = {change.storedAttribute: change.newValue, "updatedAt": after.updatedAt}
    assert after == before._replace(**rewritten)


@pytest.mark.parametrize("overridesOf", NO_CHANGE_OVERRIDES, ids=NO_CHANGE_OVERRIDE_IDS)
async def test_unchanged_fields_answer_please_modify(harness, overridesOf):
    """USR-MODIFIED-NO (lines 238-242): 400 ``Please modify to update ...``; nothing rewritten.

    No ERR-FLG is set and FNAME is only the cursor, so no field is listed as failing.
    """
    before = StoredUserOf(harness)
    request = BuildUpdate(before, overridesOf(harness, before))
    error = await UpdateExpectingError(harness.context, harness.user.userId, request)
    assert ScreenOutcome(error) == (PLEASE_MODIFY_MESSAGE, FIRST_NAME_FIELD, EDIT_FAILURE_STATUS)
    assert error.Body()[FIELD_ERRORS_KEY] == []
    assert StoredUserOf(harness) == before


async def test_replacement_password_is_hashed_as_entered(harness, testSettings):
    """A PASSWD that does not verify is a change: its hash, at BCRYPT_ROUNDS, replaces the old.

    The names and type stay as stored, and the old password no longer verifies.
    """
    before = StoredUserOf(harness)
    newPassword = await ReplacePassword(harness)
    after = StoredUserOf(harness)
    assert after.passwordHash != before.passwordHash
    assert VerifyPassword(newPassword, after.passwordHash)
    assert not VerifyPassword(harness.user.password, after.passwordHash)
    assert HashCost(after.passwordHash) == testSettings.BCRYPT_ROUNDS
    unchanged = {"passwordHash": after.passwordHash, "updatedAt": after.updatedAt}
    assert after == before._replace(**unchanged)


async def test_replaced_password_signs_on_and_the_old_one_is_refused(harness):
    """COSGN00C signs the user on with the new password; the old one is the wrong password.

    The refused sign-on counts a failure in security.login_attempts, which restoreSeedState
    removes at teardown with every other row the test committed.
    """
    newPassword = await ReplacePassword(harness)
    signedOn = await SignOnWith(harness.context, harness.user.userId, newPassword)
    assert (signedOn.user_id, signedOn.next_route) == (harness.user.userId, MENU_ROUTE)
    with pytest.raises(LegacyMessageError) as refused:
        await SignOnWith(harness.context, harness.user.userId, harness.user.password)
    expectedOutcome = (WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD, SIGN_ON_FAILURE_STATUS)
    assert ScreenOutcome(refused.value) == expectedOutcome


@pytest.mark.parametrize("refusal", PRESENCE_REFUSALS, ids=PRESENCE_REFUSAL_IDS)
async def test_presence_checks_run_in_source_order_without_a_password_check(harness, refusal):
    """The first blank input in source order (lines 179-213) is 400 at its own field."""
    before = StoredUserOf(harness)
    overrides = {fieldName: BLANK for fieldName in refusal.blankFields}
    userId = overrides.pop(USER_ID_FIELD, harness.user.userId)
    error = await UpdateExpectingError(harness.context, userId, BuildUpdate(before, overrides))
    expectedOutcome = (refusal.expectedMessage, refusal.cursorField, EDIT_FAILURE_STATUS)
    assert ScreenOutcome(error) == expectedOutcome
    assert StoredUserOf(harness) == before


async def test_unknown_user_id_is_not_found(harness):
    """READ UPDATE NOTFND: 404 ``User ID NOT found...`` at USRIDIN, and no record rewritten."""
    before = StoredUserOf(harness)
    request = BuildUpdate(before, {FIRST_NAME_FIELD: CHANGED_FIRST_NAME})
    error = await UpdateExpectingError(harness.context, UNKNOWN_USER_ID, request)
    assert ScreenOutcome(error) == (USER_NOT_FOUND_MESSAGE, USER_ID_FIELD, NOT_FOUND_STATUS)
    assert StoredUserOf(harness) == before


async def test_rewrite_notfnd_is_not_found(harness):
    """REWRITE NOTFND (lines 377-382): 404 ``User ID NOT found...``; the unit rolls back."""
    RaiseOnRepository(harness, "UpdateUser", RecordNotFoundError)
    before = StoredUserOf(harness)
    request = BuildUpdate(before, {FIRST_NAME_FIELD: CHANGED_FIRST_NAME})
    error = await UpdateExpectingError(harness.context, harness.user.userId, request)
    assert ScreenOutcome(error) == (USER_NOT_FOUND_MESSAGE, USER_ID_FIELD, NOT_FOUND_STATUS)
    assert StoredUserOf(harness) == before


@pytest.mark.parametrize("failure", UPDATE_FAILURES, ids=UPDATE_FAILURE_IDS)
async def test_file_condition_is_unable_to_update(harness, failure):
    """A lock, window or I/O failure is 503 ``Unable to Update User...`` at FNAME; no rewrite."""
    RaiseOnRepository(harness, failure.methodName, failure.errorType)
    before = StoredUserOf(harness)
    request = BuildUpdate(before, {FIRST_NAME_FIELD: CHANGED_FIRST_NAME})
    error = await UpdateExpectingError(harness.context, harness.user.userId, request)
    expectedOutcome = (UNABLE_TO_UPDATE_USER_MESSAGE, FIRST_NAME_FIELD, FILE_ERROR_STATUS)
    assert ScreenOutcome(error) == expectedOutcome
    assert StoredUserOf(harness) == before


async def test_f3_saves_every_pending_change_through_the_f5_entry_point(harness):
    """F3 performs UPDATE-USER-INFO before it returns (lines 111-119), exactly as F5 does.

    The router therefore calls cousr02c.UpdateUser for both keys; this F3 save of every field
    at once commits the whole record before the client navigates back.
    """
    newPassword = NewPassword()
    request = UserUpdateRequest(
        first_name=CHANGED_FIRST_NAME,
        last_name=CHANGED_LAST_NAME,
        password=newPassword,
        user_type=CHANGED_USER_TYPE,
    )
    response = await cousr02c.UpdateUser(harness.context, harness.user.userId, request)
    assert response.message == UpdatedMessage(harness.user.userId)
    after = StoredUserOf(harness)
    savedFields = (after.firstName, after.lastName, after.userType)
    assert savedFields == (CHANGED_FIRST_NAME, CHANGED_LAST_NAME, CHANGED_USER_TYPE)
    assert VerifyPassword(newPassword, after.passwordHash)


@pytest.mark.parametrize("produceBody", OUTCOME_BODIES, ids=OUTCOME_BODY_IDS)
async def test_no_outcome_body_carries_a_password_or_a_hash(harness, produceBody):
    """Neither the stored or typed password nor the old or new hash reaches any body."""
    hashBefore = StoredUserOf(harness).passwordHash
    outcome = await produceBody(harness)
    hashAfter = StoredUserOf(harness).passwordHash
    bodyText = json.dumps(outcome.body)
    credentials = {harness.user.password, outcome.typedPassword, hashBefore, hashAfter}
    for credential in credentials - {BLANK}:
        assert credential not in bodyText
