"""Unit tests for COUSR01C (CU01), the COUSR01 add-user screen, as app.services.users.cousr01c.

cbl/COUSR01C.cbl PROCESS-ENTER-KEY checks First Name, Last Name, User ID, Password and User
Type for presence in that order; the first one holding only spaces or nothing ends validation
with ``<Field> can NOT be empty...`` and the cursor on that field (lines 117-151). No other edit
exists, so any non-blank user type is written. WRITE-USER-SEC-FILE then writes the record with
the keyed values (lines 238-274): NORMAL answers ``User <id> has been added ...`` with the ID
up to its first space and the cursor on FNAMEL; DUPKEY or DUPREC answers
``User ID already exist...`` on USERIDL; any other condition, the NOTOPEN of the batch window
included, answers ``Unable to Add User...`` on FNAMEL. The WHEN OTHER branch of the EIBAID
EVALUATE (lines 98-102), which F12 reaches, answers CSMSG01Y's CCDA-MSG-INVALID-KEY on FNAMEL.
Expected values are typed from those lines and the AAP's port of them:
- each blank field is a 400 with its literal and cursor, before any later field is checked,
    and security.users is left unchanged;
- user types U, A, x and 9 are all stored as keyed;
- the password is stored only as a bcrypt hash of the text as entered, case kept, so its
    upper-cased form does not verify;
- a second add of one user ID is a 409 and keeps the first record;
- NOTOPEN and IOERR on USRSEC are a 503 and write nothing;
- no response or error body holds an entered password or a stored hash.

Each case calls the service against the migrated, seeded test database; restoreSeedState puts
security.users back to its ten seed users afterwards. Passwords are generated with secrets.
"""

import json
import secrets
import string
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core import messages
from app.core.errors import FileIoError, LegacyMessageError
from app.core.locks import FileNotOpenError
from app.core.security import VerifyPassword
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.schemas.users import UserCreateRequest, UserMessageResponse
from app.services.users import cousr01c

# Every case that reaches the database leaves security.users as the seed left it.
RESTORES_SEED_STATE = pytest.mark.usefixtures("restoreSeedState")

# The signed-on user of every case: CU01 is an admin-menu option (COADM02Y).
ADMIN_USER_TYPE = "A"
# BuildCreateRequest's keyed values. An ID of 8 upper-case hex digits (USERID, LENGTH 8)
# matches no seed ID; the 8-character password puts lower-case letters around 6 upper-case
# hex digits (PASSWD, LENGTH 8), so it differs from its upper-cased form.
USER_ID_HEX_BYTES = 4
CREDENTIAL_MIDDLE_HEX_BYTES = 3
TEST_FIRST_NAME = "TEST"
TEST_LAST_NAME = "USER"
REGULAR_USER_TYPE = "U"
# The other names of a second add under an existing user ID.
OTHER_FIRST_NAME = "OTHER"
OTHER_LAST_NAME = "NAME"

# SPACES OR LOW-VALUES: a field sent empty, or holding only its space padding.
BLANK_VALUES = (("empty", ""), ("spaces", " "))
# The UserCreateRequest wire names, in the order PROCESS-ENTER-KEY checks them.
REQUEST_FIELDS = ("first_name", "last_name", "user_id", "password", "user_type")


class PresenceCheck(NamedTuple):
    """One WHEN of PROCESS-ENTER-KEY: the field it tests, also its cursor, and its literal."""

    fieldName: str
    message: str


# PROCESS-ENTER-KEY lines 117-147, in source order.
PRESENCE_CHECKS = (
    PresenceCheck("first_name", "First Name can NOT be empty..."),
    PresenceCheck("last_name", "Last Name can NOT be empty..."),
    PresenceCheck("user_id", "User ID can NOT be empty..."),
    PresenceCheck("password", "Password can NOT be empty..."),
    PresenceCheck("user_type", "User Type can NOT be empty..."),
)
EDIT_FAILURE_STATUS = 400

# WRITE-USER-SEC-FILE NORMAL (lines 251-259): the STRING takes SEC-USR-ID DELIMITED BY SPACE,
# and INITIALIZE-ALL-FIELDS moves -1 to FNAMEL.
USER_ADDED_TEMPLATE = "User {userId} has been added ..."
FIRST_NAME_FIELD = "first_name"
# WRITE-USER-SEC-FILE DUPKEY/DUPREC (lines 260-266), cursor USERIDL.
USER_ID_EXISTS_MESSAGE = "User ID already exist..."
USER_ID_FIELD = "user_id"
DUPLICATE_KEY_STATUS = 409
# WRITE-USER-SEC-FILE WHEN OTHER (lines 267-273), cursor FNAMEL.
UNABLE_TO_ADD_USER_MESSAGE = "Unable to Add User..."
FILE_ERROR_STATUS = 503
# CSMSG01Y CCDA-MSG-INVALID-KEY, its PIC X(50) padding trimmed (MAIN-PARA lines 98-102).
INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below..."

# U (regular) and A (admin), and two values COUSR01C accepts though only A signs on as admin.
KEYED_USER_TYPES = ["U", "A", "x", "9"]
KEYED_USER_TYPE_IDS = ["regular-U", "admin-A", "lower-case-x", "digit-9"]


class UserIdCase(NamedTuple):
    """A keyed user ID, the ID the success literal shows and the ID security.users holds."""

    keyedUserId: str
    shownUserId: str
    storedUserId: str


# DELIMITED BY SPACE shows the ID up to its first space; the column holds it right-trimmed.
USER_ID_CASES = [
    pytest.param(UserIdCase("CU01 SP", "CU01", "CU01 SP"), id="embedded-space"),
    pytest.param(UserIdCase("CU01PAD ", "CU01PAD", "CU01PAD"), id="trailing-padding"),
    pytest.param(UserIdCase(" CU01LD", "", " CU01LD"), id="leading-space"),
]

USER_RECORD_QUERY = text(
    "SELECT user_id, first_name, last_name, password_hash, user_type"
    " FROM security.users WHERE user_id = :userId"
)
ALL_USER_RECORDS_QUERY = text(
    "SELECT user_id, first_name, last_name, password_hash, user_type"
    " FROM security.users ORDER BY user_id"
)


def NewMixedCaseCredential() -> str:
    """Return 8 characters from secrets: a lower-case letter, 6 upper-case hex digits, another."""
    return (
        secrets.choice(string.ascii_lowercase)
        + secrets.token_hex(CREDENTIAL_MIDDLE_HEX_BYTES).upper()
        + secrets.choice(string.ascii_lowercase)
    )


def BuildCreateRequest(overrides: Mapping[str, str] | None = None) -> UserCreateRequest:
    """Return a COUSR01 ENTER body that passes every edit, with ``overrides`` applied."""
    keyedFields = {
        "first_name": TEST_FIRST_NAME,
        "last_name": TEST_LAST_NAME,
        "user_id": secrets.token_hex(USER_ID_HEX_BYTES).upper(),
        "password": NewMixedCaseCredential(),
        "user_type": REGULAR_USER_TYPE,
    }
    return UserCreateRequest(**{**keyedFields, **(overrides or {})})


def UserRecords(syncEngine: Engine, userId: str) -> list[dict[str, Any]]:
    """Return every committed security.users row keyed ``userId``, read with plain SQL."""
    with syncEngine.connect() as connection:
        rows = connection.execute(USER_RECORD_QUERY, {"userId": userId}).mappings()
        return [dict(row) for row in rows]


def ReadUser(syncEngine: Engine, userId: str) -> dict[str, Any] | None:
    """Return the committed security.users row keyed ``userId``, or None when there is none."""
    with syncEngine.connect() as connection:
        row = connection.execute(USER_RECORD_QUERY, {"userId": userId}).mappings().one_or_none()
        return None if row is None else dict(row)


def StoredUsers(syncEngine: Engine) -> list[tuple[Any, ...]]:
    """Return every committed security.users row, in user-ID order."""
    with syncEngine.connect() as connection:
        return [tuple(row) for row in connection.execute(ALL_USER_RECORDS_QUERY)]


def ExpectedErrorBody(message: str, cursorField: str) -> dict[str, object]:
    """Return the legacy error body of a first-failure literal with the cursor on a field."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [cursorField],
        "cursor_field": cursorField,
    }


def AssertHoldsNoCredential(serializedBody: str, credentials: Sequence[str]) -> None:
    """Assert that no entered password and no stored hash appears in ``serializedBody``."""
    for credential in credentials:
        assert credential not in serializedBody


def PresenceCases() -> list[Any]:
    """Return each field blank alone, and blank with every later field, empty and as spaces.

    Blank later fields show a check runs before theirs; filled earlier fields show theirs pass.
    """
    cases = []
    for blankName, blankValue in BLANK_VALUES:
        for index, check in enumerate(PRESENCE_CHECKS):
            for blankFields in dict.fromkeys([(check.fieldName,), REQUEST_FIELDS[index:]]):
                scope = "only" if len(blankFields) == 1 else "from"
                caseId = f"{scope}-{check.fieldName}-{blankName}"
                overrides = dict.fromkeys(blankFields, blankValue)
                cases.append(pytest.param(overrides, check, id=caseId))
    return cases


@RESTORES_SEED_STATE
@pytest.mark.parametrize(("overrides", "presenceCheck"), PresenceCases())
async def test_first_blank_field_in_source_order_is_refused_and_nothing_written(
    makeOnlineContext, syncEngine, overrides, presenceCheck
):
    """A blank field is a 400 with its literal and cursor, and security.users is unchanged."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    usersBefore = StoredUsers(syncEngine)

    with pytest.raises(LegacyMessageError) as raised:
        await cousr01c.AddUser(context, BuildCreateRequest(overrides))

    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == ExpectedErrorBody(presenceCheck.message, presenceCheck.fieldName)
    assert StoredUsers(syncEngine) == usersBefore


@RESTORES_SEED_STATE
@pytest.mark.parametrize("userType", KEYED_USER_TYPES, ids=KEYED_USER_TYPE_IDS)
async def test_any_user_type_is_added_as_keyed(makeOnlineContext, syncEngine, userType):
    """No user-type edit exists: every non-blank type is written with the other fields."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    request = BuildCreateRequest({"user_type": userType})

    response = await cousr01c.AddUser(context, request)

    assert response == UserMessageResponse(
        message=USER_ADDED_TEMPLATE.format(userId=request.user_id),
        cursor_field=FIRST_NAME_FIELD,
    )
    storedUser = ReadUser(syncEngine, request.user_id)
    assert storedUser is not None
    storedUser.pop("password_hash")
    assert storedUser == request.model_dump(exclude={"password"})
    assert storedUser["user_type"] == userType


@RESTORES_SEED_STATE
@pytest.mark.parametrize("userIdCase", USER_ID_CASES)
async def test_added_message_shows_the_user_id_up_to_its_first_space(
    makeOnlineContext, syncEngine, userIdCase
):
    """The STRING stops at the first space of SEC-USR-ID; the record keeps the ID as keyed."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    request = BuildCreateRequest({"user_id": userIdCase.keyedUserId})

    response = await cousr01c.AddUser(context, request)

    assert response.message == USER_ADDED_TEMPLATE.format(userId=userIdCase.shownUserId)
    assert response.cursor_field == FIRST_NAME_FIELD
    storedUser = ReadUser(syncEngine, userIdCase.storedUserId)
    assert storedUser is not None
    assert storedUser["user_id"] == userIdCase.storedUserId


@RESTORES_SEED_STATE
async def test_password_is_stored_only_as_a_hash_of_the_text_as_entered(
    makeOnlineContext, syncEngine
):
    """The hash verifies the password with its case kept, never its upper-cased sign-on form."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    request = BuildCreateRequest()
    enteredPassword = request.password

    response = await cousr01c.AddUser(context, request)

    storedUser = ReadUser(syncEngine, request.user_id)
    assert storedUser is not None
    storedHash = storedUser["password_hash"]
    assert storedHash != enteredPassword
    assert VerifyPassword(enteredPassword, storedHash)
    assert enteredPassword != enteredPassword.upper()
    assert not VerifyPassword(enteredPassword.upper(), storedHash)
    AssertHoldsNoCredential(response.model_dump_json(), (enteredPassword, storedHash))


@RESTORES_SEED_STATE
async def test_second_add_of_one_user_id_is_refused_and_keeps_the_first_record(
    makeOnlineContext, syncEngine
):
    """DUPREC is a 409 on the user-ID field; the first record is neither replaced nor doubled."""
    firstRequest = BuildCreateRequest()
    await cousr01c.AddUser(await makeOnlineContext(ADMIN_USER_TYPE), firstRequest)
    firstRecord = ReadUser(syncEngine, firstRequest.user_id)
    secondRequest = BuildCreateRequest(
        {
            "user_id": firstRequest.user_id,
            "first_name": OTHER_FIRST_NAME,
            "last_name": OTHER_LAST_NAME,
            "user_type": ADMIN_USER_TYPE,
        }
    )

    with pytest.raises(LegacyMessageError) as raised:
        await cousr01c.AddUser(await makeOnlineContext(ADMIN_USER_TYPE), secondRequest)

    assert raised.value.httpStatus == DUPLICATE_KEY_STATUS
    assert raised.value.Body() == ExpectedErrorBody(USER_ID_EXISTS_MESSAGE, USER_ID_FIELD)
    assert UserRecords(syncEngine, firstRequest.user_id) == [firstRecord]
    credentials = (firstRequest.password, secondRequest.password, firstRecord["password_hash"])
    AssertHoldsNoCredential(json.dumps(raised.value.Body()), credentials)


@RESTORES_SEED_STATE
@pytest.mark.parametrize("fileError", [FileNotOpenError, FileIoError], ids=["NOTOPEN", "IOERR"])
async def test_usrsec_write_condition_is_unable_to_add_user_and_writes_nothing(
    makeOnlineContext, syncEngine, monkeypatch, fileError
):
    """NOTOPEN (the batch window) and IOERR take WHEN OTHER: a 503 on the first-name field."""
    insertStub = AsyncMock(side_effect=fileError(USER_FILE_NAME))
    monkeypatch.setattr(UserRepository, "InsertUser", insertStub)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    usersBefore = StoredUsers(syncEngine)
    request = BuildCreateRequest()

    with pytest.raises(LegacyMessageError) as raised:
        await cousr01c.AddUser(context, request)

    insertStub.assert_awaited_once()
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == ExpectedErrorBody(UNABLE_TO_ADD_USER_MESSAGE, FIRST_NAME_FIELD)
    assert StoredUsers(syncEngine) == usersBefore
    AssertHoldsNoCredential(json.dumps(raised.value.Body()), (request.password,))


def test_invalid_key_is_refused_with_the_csmsg01y_literal_on_the_first_name_field():
    """Any AID but ENTER, PF3 and PF4, F12 included, answers CCDA-MSG-INVALID-KEY with a 400."""
    assert INVALID_KEY_MESSAGE == messages.INVALID_KEY_MESSAGE

    with pytest.raises(LegacyMessageError) as raised:
        cousr01c.RejectInvalidKey()

    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == ExpectedErrorBody(INVALID_KEY_MESSAGE, FIRST_NAME_FIELD)
