"""Unit tests for app.services.auth.cosgn00c, the port of COSGN00C (transaction CC00, map COSGN00).

PROCESS-ENTER-KEY (cbl/COSGN00C.cbl lines 108-140) edits the map fields in first-failure order:
a USERID of SPACES OR LOW-VALUES gives ``Please enter User ID ...`` with the cursor on USERIDL,
then a blank PASSWD gives ``Please enter Password ...`` on PASSWDL. Both fields pass through
FUNCTION UPPER-CASE (lines 132-136) before READ-USER-SEC-FILE (lines 209-257) reads USRSEC and
compares the password: a mismatch gives ``Wrong Password. Try again ...`` (PASSWDL), NOTFND
(RESP 13) ``User not found. Try again ...`` (USERIDL) and any other RESP ``Unable to verify the
User ...`` (USERIDL). Type exactly A (COCOM01Y CDEMO-USRTYP-ADMIN) is transferred to COADM01C,
every other type to COMEN01C. F3 answers the CSMSG01Y thank-you literal. The login throttle of
AAP 0.7.1 locks an upper-cased user ID after LOGIN_MAX_ATTEMPTS failures with
``Too many failed attempts. Try again later ...``.

Every case calls the service directly with a makeOnlineContext context, never over HTTP, and
re-reads security.users and security.login_attempts with bound SQL through syncEngine. Expected
literals are transcribed from the lines above. Passwords come only from createUser, the USRSEC
image (seedUsers) or ``secrets``; blank fields are empty or spaces.
"""

import asyncio
import secrets
import string
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import NamedTuple, Protocol

import pytest
import pytest_asyncio
from sqlalchemy import Engine, Row, text

from app.core import messages
from app.core.dependencies import CurrentUser, OnlineContext
from app.core.errors import FileIoError, LegacyMessageError
from app.core.locks import FileNotOpenError
from app.core.security import HashPassword, VerifyPassword
from app.repositories.user_repository import USER_FILE_NAME, UserRepository
from app.schemas.auth import LoginRequest
from app.services.auth import cosgn00c

# PROCESS-ENTER-KEY lines 120 and 125; READ-USER-SEC-FILE lines 242, 249 and 254, verbatim.
USER_ID_REQUIRED_MESSAGE = "Please enter User ID ..."
CREDENTIAL_REQUIRED_MESSAGE = "Please enter Password ..."
USER_NOT_FOUND_MESSAGE = "User not found. Try again ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."
UNABLE_TO_VERIFY_MESSAGE = "Unable to verify the User ..."
# CSMSG01Y CCDA-MSG-THANK-YOU, its PIC X(50) padding trimmed.
THANK_YOU_MESSAGE = "Thank you for using CardDemo application..."
# AAP 0.7.1: the one literal the sign-on throttle adds.
LOCKED_MESSAGE = "Too many failed attempts. Try again later ..."

# The pages that replace XCTL PROGRAM('COADM01C') and XCTL PROGRAM('COMEN01C') (lines 230-239).
ADMIN_ROUTE = "/admin"
MENU_ROUTE = "/menu"
# MOVE -1 TO USERIDL / PASSWDL, named as the LoginRequest wire fields.
USER_ID_FIELD = "user_id"
CREDENTIAL_FIELD = "password"
SIGNON_FAILURE_STATUS = 401
LOCKED_STATUS = 429
# The WHEN OTHER branch is a file error, which answers 503 (AAP 0.6.5), not a sign-on refusal.
FILE_ERROR_STATUS = 503
# WS-USRSEC-FILE VALUE 'USRSEC  ' (line 39), the file name the file conditions carry.
USRSEC_FILE_NAME = "USRSEC"

ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
ADMIN_SEED_USER_ID = "ADMIN001"
REGULAR_SEED_USER_ID = "USER0001"
# A blank map field. LoginRequest refuses X'00' (outside the code-page-037 printable
# repertoire), so an empty or omitted field stands for LOW-VALUES.
BLANK_FIELD = ""
SPACES_FIELD = "        "
FIELD_PADDING = " "
# Markers of the blank-field table, replaced inside the test by the generated user's own ID and
# by a random upper-case password, so the table holds no password.
GENERATED_USER_ID = "generated user ID"
RANDOM_CREDENTIAL = "random password"
# User IDs inserted by the lower-case password pin: "L" and six upper-case hex digits, never a
# seed ID (ADMINnnn, USERnnnn) or a createUser ID ("T" prefix).
LOWER_CASE_USER_PREFIX = "L"
LOWER_CASE_USER_ID_HEX_BYTES = 3
# RandomUpperPassword: 8 upper-case hex digits, the PASSWD LENGTH of map COSGN00.
CREDENTIAL_HEX_BYTES = 4
# The random middle of RandomLowerPassword, between its two lower-case letters: 6 hex digits.
LOWER_CREDENTIAL_MIDDLE_HEX_BYTES = 3
# An unknown user ID: 8 upper-case hex digits, never a seed ID or a "T" or "L" generated one.
UNKNOWN_USER_ID_HEX_BYTES = 4
LOWER_CASE_USER_FIRST_NAME = "Lower"
LOWER_CASE_USER_LAST_NAME = "Case Password"
# The only keys GET /auth/me may carry: no password, hash or token.
CURRENT_USER_KEYS = frozenset({"user_id", "user_type", "first_name", "last_name", "next_route"})

ATTEMPT_ROW_QUERY = text(
    "SELECT failure_count, locked_until FROM security.login_attempts WHERE user_id = :userId"
)
STORED_USER_QUERY = text(
    "SELECT first_name, last_name, password_hash, user_type FROM security.users"
    " WHERE user_id = :userId"
)
INSERT_USER_STATEMENT = text(
    "INSERT INTO security.users (user_id, first_name, last_name, password_hash, user_type)"
    " VALUES (:userId, :firstName, :lastName, :passwordHash, :userType)"
)
DELETE_ATTEMPT_ROW_STATEMENT = text("DELETE FROM security.login_attempts WHERE user_id = :userId")


class GeneratedUser(Protocol):
    """A user createUser committed to security.users, with the password it signs on with."""

    userId: str
    password: str
    userType: str


class ImageUser(Protocol):
    """One record of the USRSEC image (copybook CSUSR01Y), each field right-trimmed."""

    userId: str
    firstName: str
    lastName: str
    password: str
    userType: str


@dataclass(frozen=True)
class ExpectedFailure:
    """A refused ENTER on COSGN00: the message line, the cursor field and the HTTP status."""

    message: str
    cursorField: str
    httpStatus: int


@dataclass(frozen=True)
class SignOnScenario:
    """A sign-on context without a session user, and a generated regular user."""

    context: OnlineContext
    user: GeneratedUser


class BlankFieldCase(NamedTuple):
    """One row of the first-failure table: the keyed fields, markers allowed, and the failure."""

    userIdText: str
    credentialText: str
    expected: ExpectedFailure


class SeedSignOnCase(NamedTuple):
    """A USRSEC image user, its stored type and the menu its sign-on opens."""

    userId: str
    userType: str
    nextRoute: str


USER_ID_REQUIRED = ExpectedFailure(USER_ID_REQUIRED_MESSAGE, USER_ID_FIELD, SIGNON_FAILURE_STATUS)
CREDENTIAL_REQUIRED = ExpectedFailure(
    CREDENTIAL_REQUIRED_MESSAGE, CREDENTIAL_FIELD, SIGNON_FAILURE_STATUS
)
USER_NOT_FOUND = ExpectedFailure(USER_NOT_FOUND_MESSAGE, USER_ID_FIELD, SIGNON_FAILURE_STATUS)
WRONG_CREDENTIAL = ExpectedFailure(
    WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD, SIGNON_FAILURE_STATUS
)
UNABLE_TO_VERIFY = ExpectedFailure(UNABLE_TO_VERIFY_MESSAGE, USER_ID_FIELD, FILE_ERROR_STATUS)
USER_ID_LOCKED = ExpectedFailure(LOCKED_MESSAGE, USER_ID_FIELD, LOCKED_STATUS)

BLANK_FIELD_CASES = [
    pytest.param(
        BlankFieldCase(BLANK_FIELD, RANDOM_CREDENTIAL, USER_ID_REQUIRED), id="empty-user-id"
    ),
    pytest.param(
        BlankFieldCase(SPACES_FIELD, RANDOM_CREDENTIAL, USER_ID_REQUIRED), id="spaces-user-id"
    ),
    pytest.param(
        BlankFieldCase(BLANK_FIELD, BLANK_FIELD, USER_ID_REQUIRED), id="both-empty-user-id-first"
    ),
    pytest.param(
        BlankFieldCase(GENERATED_USER_ID, BLANK_FIELD, CREDENTIAL_REQUIRED), id="empty-password"
    ),
    pytest.param(
        BlankFieldCase(GENERATED_USER_ID, SPACES_FIELD, CREDENTIAL_REQUIRED), id="spaces-password"
    ),
]
SEED_SIGN_ON_CASES = [
    pytest.param(
        SeedSignOnCase(ADMIN_SEED_USER_ID, ADMIN_USER_TYPE, ADMIN_ROUTE), id="admin-to-admin-menu"
    ),
    pytest.param(
        SeedSignOnCase(REGULAR_SEED_USER_ID, REGULAR_USER_TYPE, MENU_ROUTE), id="user-to-main-menu"
    ),
]
# IF CDEMO-USRTYP-ADMIN tests type exactly A: lower-case a and every other value are regular.
NEXT_ROUTE_CASES = [
    pytest.param(ADMIN_USER_TYPE, ADMIN_ROUTE, id="A"),
    pytest.param(REGULAR_USER_TYPE, MENU_ROUTE, id="U"),
    pytest.param("u", MENU_ROUTE, id="lower-u"),
    pytest.param("a", MENU_ROUTE, id="lower-a"),
    pytest.param("X", MENU_ROUTE, id="X"),
    pytest.param(FIELD_PADDING, MENU_ROUTE, id="blank"),
]
FILE_CONDITION_CASES = [
    pytest.param(FileIoError, id="ioerr"),
    pytest.param(FileNotOpenError, id="notopen-batch-window"),
]
# Each transcription beside the service or shared constant it must equal.
TRANSCRIBED_LITERAL_CASES = [
    pytest.param(USER_ID_REQUIRED_MESSAGE, cosgn00c.USER_ID_REQUIRED_MESSAGE, id="user-id"),
    pytest.param(CREDENTIAL_REQUIRED_MESSAGE, cosgn00c.CREDENTIAL_REQUIRED_MESSAGE, id="password"),
    pytest.param(USER_NOT_FOUND_MESSAGE, cosgn00c.USER_NOT_FOUND_MESSAGE, id="not-found"),
    pytest.param(WRONG_CREDENTIAL_MESSAGE, cosgn00c.WRONG_CREDENTIAL_MESSAGE, id="wrong-password"),
    pytest.param(UNABLE_TO_VERIFY_MESSAGE, cosgn00c.UNABLE_TO_VERIFY_MESSAGE, id="unable"),
    pytest.param(THANK_YOU_MESSAGE, messages.THANK_YOU_MESSAGE, id="thank-you"),
    pytest.param(LOCKED_MESSAGE, messages.LOGIN_LOCKED_MESSAGE, id="locked"),
    pytest.param(USRSEC_FILE_NAME, USER_FILE_NAME, id="usrsec-file"),
]


def RandomUpperPassword() -> str:
    """Return a random 8-character upper-case password, as COSGN00C compares it."""
    return secrets.token_hex(CREDENTIAL_HEX_BYTES).upper()


def RandomLowerPassword() -> str:
    """Return a random 8-character password that starts and ends with a lower-case letter."""
    return (
        secrets.choice(string.ascii_lowercase)
        + secrets.token_hex(LOWER_CREDENTIAL_MIDDLE_HEX_BYTES)
        + secrets.choice(string.ascii_lowercase)
    )


def OtherPassword(password: str) -> str:
    """Return a random upper-case password that differs from ``password``."""
    otherPassword = RandomUpperPassword()
    while otherPassword == password:
        otherPassword = RandomUpperPassword()
    return otherPassword


def CorrectPasswordRequest(user: GeneratedUser) -> LoginRequest:
    """Return the COSGN00 fields of ``user`` keyed with the password it was created with."""
    return LoginRequest(user_id=user.userId, password=user.password)


def WrongPasswordRequest(user: GeneratedUser) -> LoginRequest:
    """Return the COSGN00 fields of ``user`` keyed with a random password other than its own."""
    return LoginRequest(user_id=user.userId, password=OtherPassword(user.password))


def KeyedFields(scenario: SignOnScenario, blankFieldCase: BlankFieldCase) -> LoginRequest:
    """Return the COSGN00 fields of a blank-field case with its markers replaced."""
    keyedUserId = blankFieldCase.userIdText
    if keyedUserId == GENERATED_USER_ID:
        keyedUserId = scenario.user.userId
    keyedCredential = blankFieldCase.credentialText
    if keyedCredential == RANDOM_CREDENTIAL:
        keyedCredential = RandomUpperPassword()
    return LoginRequest(user_id=keyedUserId, password=keyedCredential)


def SeedUserOf(seedUsers: tuple[ImageUser, ...], userId: str) -> ImageUser:
    """Return the USRSEC image record of ``userId``."""
    return next(seedUser for seedUser in seedUsers if seedUser.userId == userId)


def ReadAttemptRow(syncEngine: Engine, userId: str) -> Row | None:
    """Return the committed security.login_attempts row of ``userId``, or None."""
    with syncEngine.connect() as connection:
        return connection.execute(ATTEMPT_ROW_QUERY, {"userId": userId}).one_or_none()


def ReadStoredUser(syncEngine: Engine, userId: str) -> Row:
    """Return the committed security.users row of ``userId``."""
    with syncEngine.connect() as connection:
        return connection.execute(STORED_USER_QUERY, {"userId": userId}).one()


def InsertLowerCasePasswordUser(syncEngine: Engine, passwordHash: str) -> str:
    """Commit a regular user whose stored hash is ``passwordHash``; return its user ID.

    Stands for a user COUSR01C added with a lower-case password, which it keeps as entered.
    """
    userId = LOWER_CASE_USER_PREFIX + secrets.token_hex(LOWER_CASE_USER_ID_HEX_BYTES).upper()
    userValues = {
        "userId": userId,
        "firstName": LOWER_CASE_USER_FIRST_NAME,
        "lastName": LOWER_CASE_USER_LAST_NAME,
        "passwordHash": passwordHash,
        "userType": REGULAR_USER_TYPE,
    }
    with syncEngine.begin() as connection:
        connection.execute(INSERT_USER_STATEMENT, userValues)
    return userId


def FailingGetUser(errorType: Callable[[str], Exception]) -> Callable[..., Awaitable[None]]:
    """Return a stand-in for UserRepository.GetUser that raises ``errorType`` on USRSEC."""

    async def RaiseFileCondition(repository: UserRepository, userId: str) -> None:
        raise errorType(USRSEC_FILE_NAME)

    return RaiseFileCondition


async def SignOnContext(
    makeOnlineContext: Callable[[str], Awaitable[OnlineContext]],
) -> OnlineContext:
    """Return a context of makeOnlineContext without a session user, as POST /auth/login has."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    context.user = None
    return context


async def AssertSignOnFails(
    context: OnlineContext, request: LoginRequest, expected: ExpectedFailure
) -> None:
    """Press ENTER on COSGN00 and assert the refusal: status, message line and cursor field."""
    with pytest.raises(LegacyMessageError) as excInfo:
        await cosgn00c.SignOn(context, request)
    errorBody = excInfo.value.Body()
    assert excInfo.value.httpStatus == expected.httpStatus
    assert errorBody["message"] == expected.message
    assert errorBody["cursor_field"] == expected.cursorField


@pytest_asyncio.fixture(name="signOnScenario")
async def SignOnScenarioFixture(
    makeOnlineContext: Callable[[str], Awaitable[OnlineContext]],
    createUser: Callable[[str], GeneratedUser],
) -> SignOnScenario:
    """Return a sign-on context and a regular user createUser committed off the event loop."""
    context = await SignOnContext(makeOnlineContext)
    user = await asyncio.to_thread(createUser, REGULAR_USER_TYPE)
    return SignOnScenario(context=context, user=user)


@pytest.fixture(name="throttleCleanup")
def ThrottleCleanupFixture(syncEngine: Engine) -> Iterator[list[str]]:
    """Yield a list of user IDs; delete their security.login_attempts rows at teardown."""
    throttledUserIds: list[str] = []
    yield throttledUserIds
    with syncEngine.begin() as connection:
        for throttledUserId in throttledUserIds:
            connection.execute(DELETE_ATTEMPT_ROW_STATEMENT, {"userId": throttledUserId})


# --- First-failure literals ------------------------------------------------------------------
@pytest.mark.parametrize(("transcribedValue", "serviceValue"), TRANSCRIBED_LITERAL_CASES)
def test_service_literals_equal_the_cobol_transcription(transcribedValue, serviceValue):
    assert serviceValue == transcribedValue


@pytest.mark.parametrize("blankFieldCase", BLANK_FIELD_CASES)
async def test_blank_field_gives_its_literal_with_the_user_id_edited_first(
    signOnScenario, syncEngine, blankFieldCase
):
    request = KeyedFields(signOnScenario, blankFieldCase)
    await AssertSignOnFails(signOnScenario.context, request, blankFieldCase.expected)
    # The refusal counts once against the upper-cased, right-trimmed user ID it was keyed with.
    attemptRow = ReadAttemptRow(syncEngine, request.user_id.rstrip(FIELD_PADDING))
    assert attemptRow is not None
    assert attemptRow.failure_count == 1


async def test_unknown_user_id_gives_user_not_found_on_the_user_id(makeOnlineContext, syncEngine):
    context = await SignOnContext(makeOnlineContext)
    unknownUserId = secrets.token_hex(UNKNOWN_USER_ID_HEX_BYTES).upper()
    request = LoginRequest(user_id=unknownUserId, password=RandomUpperPassword())
    await AssertSignOnFails(context, request, USER_NOT_FOUND)
    attemptRow = ReadAttemptRow(syncEngine, unknownUserId)
    assert attemptRow is not None
    assert attemptRow.failure_count == 1


async def test_wrong_password_gives_its_literal_on_the_password_and_keeps_the_hash(
    signOnScenario, syncEngine
):
    user = signOnScenario.user
    storedHash = ReadStoredUser(syncEngine, user.userId).password_hash
    await AssertSignOnFails(signOnScenario.context, WrongPasswordRequest(user), WRONG_CREDENTIAL)
    assert ReadStoredUser(syncEngine, user.userId).password_hash == storedHash
    attemptRow = ReadAttemptRow(syncEngine, user.userId)
    assert attemptRow is not None
    assert attemptRow.failure_count == 1


@pytest.mark.parametrize("errorType", FILE_CONDITION_CASES)
async def test_usrsec_file_condition_gives_unable_to_verify_on_the_user_id(
    signOnScenario, syncEngine, monkeypatch, errorType
):
    # The WHEN OTHER branch answers 503, the file-error status of AAP 0.6.5, not the 401 of a
    # sign-on refusal; it still counts as the attempt's failure.
    monkeypatch.setattr(UserRepository, "GetUser", FailingGetUser(errorType))
    user = signOnScenario.user
    await AssertSignOnFails(signOnScenario.context, CorrectPasswordRequest(user), UNABLE_TO_VERIFY)
    attemptRow = ReadAttemptRow(syncEngine, user.userId)
    assert attemptRow is not None
    assert attemptRow.failure_count == 1


# --- Success and routing ---------------------------------------------------------------------
@pytest.mark.parametrize("seedSignOnCase", SEED_SIGN_ON_CASES)
async def test_seed_user_signs_on_with_its_image_password_to_its_menu(
    makeOnlineContext, seedUsers, syncEngine, seedSignOnCase
):
    seedUser = SeedUserOf(seedUsers, seedSignOnCase.userId)
    context = await SignOnContext(makeOnlineContext)
    request = LoginRequest(user_id=seedUser.userId, password=seedUser.password)
    response = await cosgn00c.SignOn(context, request)
    storedUser = ReadStoredUser(syncEngine, seedSignOnCase.userId)
    assert response.user_id == seedSignOnCase.userId
    assert response.user_type == seedSignOnCase.userType
    assert response.next_route == seedSignOnCase.nextRoute
    assert response.first_name == storedUser.first_name.rstrip(FIELD_PADDING)
    assert response.last_name == storedUser.last_name.rstrip(FIELD_PADDING)


async def test_lower_case_user_id_and_password_are_upper_cased_and_sign_on(
    makeOnlineContext, seedUsers
):
    seedUser = SeedUserOf(seedUsers, REGULAR_SEED_USER_ID)
    context = await SignOnContext(makeOnlineContext)
    request = LoginRequest(user_id=seedUser.userId.lower(), password=seedUser.password.lower())
    response = await cosgn00c.SignOn(context, request)
    assert response.user_id == REGULAR_SEED_USER_ID
    assert response.next_route == MENU_ROUTE


@pytest.mark.parametrize(("userType", "nextRoute"), NEXT_ROUTE_CASES)
def test_only_type_exactly_a_opens_the_admin_menu(userType, nextRoute):
    assert cosgn00c.NextRouteFor(userType) == nextRoute


async def test_lower_case_stored_password_never_matches_the_upper_cased_input(
    makeOnlineContext, syncEngine, testSettings, restoreSeedState
):
    lowerPassword = RandomLowerPassword()
    passwordHash = HashPassword(lowerPassword, testSettings.BCRYPT_ROUNDS)
    userId = InsertLowerCasePasswordUser(syncEngine, passwordHash)
    assert VerifyPassword(lowerPassword, passwordHash)
    context = await SignOnContext(makeOnlineContext)
    request = LoginRequest(user_id=userId, password=lowerPassword)
    await AssertSignOnFails(context, request, WRONG_CREDENTIAL)


async def test_successful_sign_on_leaves_no_attempt_row_and_keeps_the_hash(
    signOnScenario, syncEngine
):
    user = signOnScenario.user
    storedHash = ReadStoredUser(syncEngine, user.userId).password_hash
    response = await cosgn00c.SignOn(signOnScenario.context, CorrectPasswordRequest(user))
    assert response.user_id == user.userId
    assert response.user_type == REGULAR_USER_TYPE
    assert response.next_route == MENU_ROUTE
    assert ReadAttemptRow(syncEngine, user.userId) is None
    assert ReadStoredUser(syncEngine, user.userId).password_hash == storedHash


# --- Throttle, sign-off and current user -----------------------------------------------------
async def test_limit_of_wrong_passwords_locks_the_user_id_against_the_right_one(
    signOnScenario, syncEngine, throttleCleanup
):
    context, user = signOnScenario.context, signOnScenario.user
    throttleCleanup.append(user.userId)
    maxAttempts = context.settings.LOGIN_MAX_ATTEMPTS
    for _ in range(maxAttempts):
        await AssertSignOnFails(context, WrongPasswordRequest(user), WRONG_CREDENTIAL)
    await AssertSignOnFails(context, CorrectPasswordRequest(user), USER_ID_LOCKED)
    # The locked attempt is not counted: the row stays at the limit, with its lock.
    attemptRow = ReadAttemptRow(syncEngine, user.userId)
    assert attemptRow is not None
    assert attemptRow.failure_count == maxAttempts
    assert attemptRow.locked_until is not None


async def test_success_short_of_the_limit_resets_and_the_next_failure_counts_one(
    signOnScenario, syncEngine, throttleCleanup
):
    context, user = signOnScenario.context, signOnScenario.user
    throttleCleanup.append(user.userId)
    failuresShortOfTheLimit = context.settings.LOGIN_MAX_ATTEMPTS - 1
    for _ in range(failuresShortOfTheLimit):
        await AssertSignOnFails(context, WrongPasswordRequest(user), WRONG_CREDENTIAL)
    assert ReadAttemptRow(syncEngine, user.userId).failure_count == failuresShortOfTheLimit
    response = await cosgn00c.SignOn(context, CorrectPasswordRequest(user))
    assert response.user_id == user.userId
    assert ReadAttemptRow(syncEngine, user.userId) is None
    await AssertSignOnFails(context, WrongPasswordRequest(user), WRONG_CREDENTIAL)
    attemptRow = ReadAttemptRow(syncEngine, user.userId)
    assert attemptRow is not None
    assert attemptRow.failure_count == 1
    assert attemptRow.locked_until is None


def test_sign_off_answers_the_thank_you_literal():
    assert cosgn00c.SignOff().message == THANK_YOU_MESSAGE


@pytest.mark.parametrize(
    ("userType", "nextRoute"),
    [
        pytest.param(ADMIN_USER_TYPE, ADMIN_ROUTE, id="admin"),
        pytest.param(REGULAR_USER_TYPE, MENU_ROUTE, id="regular"),
    ],
)
async def test_current_user_is_the_session_user_with_its_own_menu(
    makeOnlineContext, userType, nextRoute
):
    context = await makeOnlineContext(userType)
    response = await cosgn00c.DescribeCurrentUser(context)
    assert response.user_id == context.user.user_id
    assert response.user_type == userType
    assert response.next_route == nextRoute
    # The session user has no USRSEC record, so the display names are blank.
    assert (response.first_name, response.last_name) == (BLANK_FIELD, BLANK_FIELD)
    assert set(response.model_dump()) == CURRENT_USER_KEYS


async def test_current_user_display_names_come_from_its_usrsec_record(makeOnlineContext, seedUsers):
    seedUser = SeedUserOf(seedUsers, ADMIN_SEED_USER_ID)
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    context.user = CurrentUser(user_id=seedUser.userId, user_type=seedUser.userType)
    response = await cosgn00c.DescribeCurrentUser(context)
    assert (response.first_name, response.last_name) == (seedUser.firstName, seedUser.lastName)
    assert response.next_route == ADMIN_ROUTE


async def test_current_user_without_a_session_user_is_refused_with_401(makeOnlineContext):
    context = await SignOnContext(makeOnlineContext)
    with pytest.raises(LegacyMessageError) as excInfo:
        await cosgn00c.DescribeCurrentUser(context)
    assert excInfo.value.httpStatus == SIGNON_FAILURE_STATUS
    assert excInfo.value.Body()["message"] == BLANK_FIELD
