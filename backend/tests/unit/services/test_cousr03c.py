"""Unit tests for COUSR03C (CU03), the delete-user screen of map COUSR03.

cbl/COUSR03C.cbl PROCESS-ENTER-KEY (lines 142-169) and DELETE-USER-INFO (lines 174-192) refuse a
blank USRIDIN with ``User ID can NOT be empty...`` and the cursor on the user ID. ENTER then runs
READ-USER-SEC-FILE (lines 267-300): NORMAL shows the names and type with ``Press PF5 key to
delete this user ...``, NOTFND answers ``User ID NOT found...`` with the cursor on the user ID,
and WHEN OTHER ``Unable to lookup User...`` with the cursor on the first name (FNAMEL, line 298).
F5 runs DELETE-USER-SEC-FILE (lines 305-336): NORMAL builds ``User <id> has been deleted ...``
from SEC-USR-ID DELIMITED BY SPACE, NOTFND answers ``User ID NOT found...``, and WHEN OTHER
``Unable to Update User...`` with the cursor on the first name (line 334).

The services are called directly with an admin context. Users come from ``createUser``, and the
database is read with plain bound SQL through ``syncEngine``. Each fetch that reaches
READ-USER-SEC-FILE reads every column of every security.users record, hash and audit timestamps
included, before the call, takes its expected fields from that before-image only, and finds that
state unchanged after the call, successful or refused. A batch-window NOTOPEN and an I/O error
are injected by replacing a UserRepository method with an AsyncMock that raises them.
Every test that can delete is restored to the seed snapshot by ``restoreSeedState``, and the ten
seed users of the USRSEC image are counted after each delete.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError, RepositoryError
from app.core.locks import FileNotOpenError
from app.repositories.user_repository import UserRepository
from app.services.users import cousr03c

# WS-USRSEC-FILE (line 39), trimmed: the CICS file every condition names.
USRSEC_FILE = "USRSEC"
# USRIDIN LENGTH=8 (bms/COUSR03.bms); SEC-USR-ID PIC X(08) (cpy/CSUSR01Y.cpy).
USER_ID_LENGTH = 8
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
# No seed (ADMIN00n, USER000n) or generated (T + 6 hex digits) user has this ID.
UNKNOWN_USER_ID = "ZZNOUSER"
BLANK_USER_IDS = ["", " " * USER_ID_LENGTH]
BLANK_USER_ID_IDS = ["empty", "eight-spaces"]

# COUSR03C message literals, verbatim (lines 147, 283, 289, 296, 318-320, 325, 332).
USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty..."
PRESS_PF5_TO_DELETE_MESSAGE = "Press PF5 key to delete this user ..."
USER_NOT_FOUND_MESSAGE = "User ID NOT found..."
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
UNABLE_TO_UPDATE_USER_MESSAGE = "Unable to Update User..."
DELETED_TEMPLATE = "User {} has been deleted ..."

# MOVE -1 TO USRIDINL (user ID) and MOVE -1 TO FNAMEL (first name).
USER_ID_FIELD = "user_id"
FIRST_NAME_FIELD = "first_name"
EDIT_FAILURE_STATUS = 400
NOT_FOUND_STATUS = 404
FILE_ERROR_STATUS = 503

# The map shows SEC-USR-FNAME, SEC-USR-LNAME and SEC-USR-TYPE; it has no password field.
PASSWORD_KEYS = ("password", "password_hash")
# The security.users columns the fetch moves to the map: the keyed ID, the names and the type.
DISPLAYED_COLUMNS = ("user_id", "first_name", "last_name", "user_type")
SEED_USER_COUNT = 10
SEED_ADMIN_PATTERN = "ADMIN%"
SEED_REGULAR_PATTERN = "USER%"

COUNT_USER_SQL = text("SELECT count(*) FROM security.users WHERE user_id = :userId")
READ_USERS_STATE_SQL = text(
    "SELECT user_id, user_id_sort, first_name, last_name, password_hash, user_type,"
    " created_at, updated_at FROM security.users"
)
COUNT_SEED_USERS_SQL = text(
    "SELECT count(*) FROM security.users"
    " WHERE user_id LIKE :adminPattern OR user_id LIKE :regularPattern"
)

# The conditions that reach a WHEN OTHER branch: the batch window's NOTOPEN and IOERR.
FILE_CONDITIONS = [FileNotOpenError, FileIoError]
FILE_CONDITION_IDS = ["batch-window-notopen", "io-error"]


def AsKeyed(userId: str) -> str:
    """Return the user ID as created, shorter than the 8-character USRIDIN field."""
    return userId


def PaddedToField(userId: str) -> str:
    """Return the user ID space-padded to USRIDIN's 8 characters, as SEC-USR-ID holds it."""
    return userId.ljust(USER_ID_LENGTH)


KEY_FORMS = [AsKeyed, PaddedToField]
KEY_FORM_IDS = ["as-keyed", "space-padded"]
SERVICE_OPERATIONS = [cousr03c.FetchUserForDelete, cousr03c.DeleteUser]
SERVICE_OPERATION_IDS = ["enter-fetch", "pf5-delete"]


def RowExists(syncEngine: Engine, userId: str) -> bool:
    """Return whether security.users holds the record keyed ``userId``, read with bound SQL."""
    with syncEngine.connect() as connection:
        return connection.execute(COUNT_USER_SQL, {"userId": userId}).scalar_one() > 0


def ReadUsersState(syncEngine: Engine) -> dict[str, dict[str, object]]:
    """Return every column of every security.users record, keyed by user ID, with plain SQL.

    The hash, the generated sort key and both audit timestamps are included, so an insert, a
    delete or a rewrite of any column changes the result.
    """
    with syncEngine.connect() as connection:
        userRecords = connection.execute(READ_USERS_STATE_SQL).mappings()
        return {userRecord["user_id"]: dict(userRecord) for userRecord in userRecords}


def DisplayedFields(userRecord: dict[str, object]) -> dict[str, object]:
    """Return the ID, names and type of ``userRecord``: the fields the COUSR03 map shows."""
    return {column: userRecord[column] for column in DISPLAYED_COLUMNS}


def SeedUserCount(syncEngine: Engine) -> int:
    """Return how many of the USRSEC image's ADMIN00n and USER000n users security.users holds."""
    patterns = {"adminPattern": SEED_ADMIN_PATTERN, "regularPattern": SEED_REGULAR_PATTERN}
    with syncEngine.connect() as connection:
        return connection.execute(COUNT_SEED_USERS_SQL, patterns).scalar_one()


def LegacyBody(message: str, cursorField: str) -> dict[str, object]:
    """Return the four standard keys of a first-failure error body with its cursor field."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [cursorField],
        "cursor_field": cursorField,
    }


def ReplaceRepositoryMethod(
    monkeypatch: pytest.MonkeyPatch, methodName: str, condition: type[RepositoryError] | None
) -> AsyncMock:
    """Replace UserRepository.<methodName> with an AsyncMock raising ``condition`` on USRSEC.

    A ``condition`` of None makes a spy that returns None. The mock is a class attribute but no
    descriptor, so it is awaited with the method's arguments without the repository itself.
    """
    sideEffect = condition(USRSEC_FILE) if condition is not None else None
    stubMethod = AsyncMock(side_effect=sideEffect, return_value=None)
    monkeypatch.setattr(UserRepository, methodName, stubMethod)
    return stubMethod


@pytest.fixture(name="failingRead", params=FILE_CONDITIONS, ids=FILE_CONDITION_IDS)
def FailingReadFixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncMock:
    """Make UserRepository.GetUser raise each WHEN OTHER condition; return the stub."""
    return ReplaceRepositoryMethod(monkeypatch, "GetUser", request.param)


@pytest.fixture(name="failingDelete", params=FILE_CONDITIONS, ids=FILE_CONDITION_IDS)
def FailingDeleteFixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncMock:
    """Make UserRepository.DeleteUser raise each WHEN OTHER condition; return the stub."""
    return ReplaceRepositoryMethod(monkeypatch, "DeleteUser", request.param)


# --- ENTER: PROCESS-ENTER-KEY and READ-USER-SEC-FILE ---------------------------------------------
@pytest.mark.parametrize("keyForm", KEY_FORMS, ids=KEY_FORM_IDS)
async def test_fetch_shows_the_stored_user_with_the_pf5_prompt(
    syncEngine, createUser, makeOnlineContext, keyForm
):
    """NORMAL: the stored names and type with the PF5 prompt, never a password or hash.

    The expected fields come from the record as it stood before the fetch, and every record,
    hash and audit timestamps included, is unchanged after it.
    """
    user = createUser(REGULAR_USER_TYPE)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    usersBefore = ReadUsersState(syncEngine)
    fetched = await cousr03c.FetchUserForDelete(context, keyForm(user.userId))
    fetchedFields = fetched.model_dump()
    assert fetchedFields == {
        **DisplayedFields(usersBefore[user.userId]),
        "message": PRESS_PF5_TO_DELETE_MESSAGE,
    }
    assert fetched.user_type == REGULAR_USER_TYPE
    assert not set(PASSWORD_KEYS) & set(fetchedFields)
    assert ReadUsersState(syncEngine) == usersBefore


@pytest.mark.parametrize("blankUserId", BLANK_USER_IDS, ids=BLANK_USER_ID_IDS)
@pytest.mark.parametrize("operation", SERVICE_OPERATIONS, ids=SERVICE_OPERATION_IDS)
async def test_blank_user_id_is_refused_before_any_usrsec_access(
    makeOnlineContext, monkeypatch, operation, blankUserId
):
    """USRIDINI = SPACES: 400 ``User ID can NOT be empty...``, cursor on the user ID, no I/O."""
    readSpy = ReplaceRepositoryMethod(monkeypatch, "GetUser", None)
    deleteSpy = ReplaceRepositoryMethod(monkeypatch, "DeleteUser", None)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    with pytest.raises(LegacyMessageError) as raised:
        await operation(context, blankUserId)
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == LegacyBody(USER_ID_EMPTY_MESSAGE, USER_ID_FIELD)
    readSpy.assert_not_awaited()
    deleteSpy.assert_not_awaited()


async def test_fetch_of_an_unknown_user_id_is_not_found(syncEngine, makeOnlineContext):
    """NOTFND: 404 ``User ID NOT found...``, cursor on the user ID; nothing written or created."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    usersBefore = ReadUsersState(syncEngine)
    with pytest.raises(LegacyMessageError) as raised:
        await cousr03c.FetchUserForDelete(context, UNKNOWN_USER_ID)
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert raised.value.Body() == LegacyBody(USER_NOT_FOUND_MESSAGE, USER_ID_FIELD)
    assert not RowExists(syncEngine, UNKNOWN_USER_ID)
    assert ReadUsersState(syncEngine) == usersBefore


async def test_fetch_file_condition_answers_unable_to_lookup_user(
    syncEngine, createUser, makeOnlineContext, failingRead
):
    """WHEN OTHER (line 296): 503 ``Unable to lookup User...``, cursor on the first name.

    Every record, the one keyed included, is unchanged after the refused fetch.
    """
    user = createUser(REGULAR_USER_TYPE)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    usersBefore = ReadUsersState(syncEngine)
    with pytest.raises(LegacyMessageError) as raised:
        await cousr03c.FetchUserForDelete(context, user.userId)
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == LegacyBody(UNABLE_TO_LOOKUP_USER_MESSAGE, FIRST_NAME_FIELD)
    failingRead.assert_awaited_once_with(user.userId)
    assert ReadUsersState(syncEngine) == usersBefore


# --- F5: DELETE-USER-INFO and DELETE-USER-SEC-FILE -----------------------------------------------
@pytest.mark.usefixtures("restoreSeedState")
@pytest.mark.parametrize("keyForm", KEY_FORMS, ids=KEY_FORM_IDS)
async def test_delete_removes_the_user_and_names_it(
    syncEngine, createUser, makeOnlineContext, keyForm
):
    """NORMAL: ``User <id> has been deleted ...``, the ID up to its first space; row gone."""
    user = createUser(REGULAR_USER_TYPE)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    deleted = await cousr03c.DeleteUser(context, keyForm(user.userId))
    assert deleted.message == DELETED_TEMPLATE.format(user.userId)
    assert deleted.cursor_field == USER_ID_FIELD
    assert not RowExists(syncEngine, user.userId)
    assert SeedUserCount(syncEngine) == SEED_USER_COUNT


@pytest.mark.usefixtures("restoreSeedState")
async def test_deleting_the_same_user_again_is_not_found(syncEngine, createUser, makeOnlineContext):
    """NOTFND on the second DELETE: 404 ``User ID NOT found...``, cursor on the user ID."""
    user = createUser(REGULAR_USER_TYPE)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    await cousr03c.DeleteUser(context, user.userId)
    with pytest.raises(LegacyMessageError) as raised:
        await cousr03c.DeleteUser(context, user.userId)
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert raised.value.Body() == LegacyBody(USER_NOT_FOUND_MESSAGE, USER_ID_FIELD)
    assert not RowExists(syncEngine, user.userId)
    assert SeedUserCount(syncEngine) == SEED_USER_COUNT


@pytest.mark.usefixtures("restoreSeedState")
async def test_delete_file_condition_answers_unable_to_update_and_keeps_the_user(
    syncEngine, createUser, makeOnlineContext, failingDelete
):
    """WHEN OTHER (line 332): 503 ``Unable to Update User...``, cursor on the first name."""
    user = createUser(REGULAR_USER_TYPE)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    with pytest.raises(LegacyMessageError) as raised:
        await cousr03c.DeleteUser(context, user.userId)
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == LegacyBody(UNABLE_TO_UPDATE_USER_MESSAGE, FIRST_NAME_FIELD)
    failingDelete.assert_awaited_once()
    assert failingDelete.await_args.args[1] == user.userId
    assert RowExists(syncEngine, user.userId)
    assert SeedUserCount(syncEngine) == SEED_USER_COUNT
