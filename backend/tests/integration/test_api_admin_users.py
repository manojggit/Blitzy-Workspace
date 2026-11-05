"""Integration tests of CU00-CU03 user administration: COUSR00C, COUSR01C, COUSR02C, COUSR03C.

Every case goes through the ASGI app at /api/v1/admin/users against the migrated test database,
with the clients, generated sign-on users and seed restore of tests/conftest.py, except the
page-walk controls, which answer from a scripted CU00 over httpx.MockTransport:

- CU00 (COUSR00C, map COUSR00): pages of at most 10 rows in EBCDIC user-ID order, which differs
  from ASCII order; the F7 and F8 refusals at either end; the inclusive search (STARTBR GTEQ)
  that resets the page number; the U and D selection routes, taken before any search key, and
  the invalid-code literal, after which the page is browsed from the search key. The F8 walk
  to the bottom literal is bounded by the pages the stored users fill and must pass the last
  key on every page, so a paging regression fails with its page history instead of hanging.
- CU01 (COUSR01C, map COUSR01): presence checks in source order, the first blank field ending
  validation; any user type stored as entered; the password hashed as entered, so a lower-case
  one never signs on through COSGN00C's upper-cased input; the duplicate key; the BMS length.
- CU02 (COUSR02C, map COUSR02): the fetch prompt, and the password semantics under hashing: a
  blank or re-entered password keeps the hash, a new one replaces it, and no response carries a
  password or a hash.
- CU03 (COUSR03C, map COUSR03): the delete prompt and the delete, and a deleted user's session
  holding until it expires while a new sign-on is refused.
- Guards: the admin role, the session cookie and the CSRF Origin check on every endpoint.

The legacy literals are pinned verbatim from the COBOL source rather than imported from the
services, so a service literal that drifts fails here. Every password is generated at run time.
"""

import secrets
import string
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple, NoReturn

import httpx
import pytest
from sqlalchemy import Engine, text

from app.core.security import VerifyPassword
from app.schemas.users import UserFetchScreen
from app.services.users.cousr00c import PAGE_SIZE

# pytest collects module marks only from this name, so it keeps pytest's spelling rather than
# the MK_Modernization upper-case constant form.
pytestmark = pytest.mark.usefixtures("restoreSeedState")

USERS_URL = "/api/v1/admin/users"
SELECTION_URL = USERS_URL + "/selection"
LOGIN_URL = "/api/v1/auth/login"
MAIN_MENU_URL = "/api/v1/menu/main"
# A blank USRIDIN, sent as one path segment holding a space.
BLANK_USER_ID_SEGMENT = "%20"

HTTP_OK = 200
HTTP_EDIT_FAILURE = 400
HTTP_SIGN_ON_FAILURE = 401
HTTP_ACCESS_REFUSED = 403
HTTP_NOT_FOUND = 404
HTTP_DUPLICATE_KEY = 409
HTTP_MALFORMED = 422

# The four keys of every legacy error body (schemas.common.LegacyErrorResponse).
LEGACY_ERROR_KEYS = frozenset({"message", "info_message", "field_errors", "cursor_field"})
# The JSON key fragments no CU02 response may carry.
SECRET_KEY_FRAGMENTS = ("password", "hash")

# A seeded regular user of data/EBCDIC/AWS.M2.CARDDEMO.USRSEC.PS, used as a key only.
SEED_REGULAR_USER_ID = "USER0001"
SEED_ADMIN_USER_ID = "ADMIN001"
REGULAR_USER_TYPE = "U"
# Generated IDs are 7 characters, upper case, prefixed N: no seed (ADMIN, USER) or conftest
# (T) user ID collides with one.
GENERATED_USER_ID_PREFIX = "N"
USER_ID_HEX_BYTES = 3
# USERID, LENGTH=8 (bms/COUSR01.bms): one character more is beyond the field.
OVERLONG_USER_ID_LENGTH = 9
OVERLONG_PADDING = "X"
# Passwords fill PASSWD, LENGTH=8: four random bytes as eight hexadecimal digits.
PASSWORD_HEX_BYTES = 4
ADDED_FIRST_NAME = "Integration"
ADDED_LAST_NAME = "Tester"
RENAMED_FIRST_NAME = "Renamed"
GUARDED_NAME = "Guarded"
# The user types COUSR01C accepts although only A signs on as an admin.
UNRESTRICTED_USER_TYPES = ("Z", "u")

EBCDIC_CODEC = "cp037"
FORWARD_DIRECTION = "forward"
BACKWARD_DIRECTION = "backward"
# Added beside the seeded ADMIN001. Code page 037 sorts lower case (X'81') before upper case
# (X'C1') before digits (X'F1'), so their EBCDIC order is EXPECTED_MIXED_ORDER, which is not
# their ASCII order (1A, A1, AA, ADMIN001, a1).
MIXED_KEYS = ("a1", "AA", "A1", "1A")
EXPECTED_MIXED_ORDER = ("a1", "AA", SEED_ADMIN_USER_ID, "A1", "1A")
# STARTBR GTEQ: an exact key starts there; ADMIN00A sorts before ADMIN001 in EBCDIC (A, X'C1',
# before 1, X'F1'), whereas ASCII would place it after ADMIN005.
SEARCH_CASES = (
    pytest.param("ADMIN003", "ADMIN003", id="exact-key"),
    pytest.param("ADMIN00A", SEED_ADMIN_USER_ID, id="between-keys-ebcdic"),
)
# A page number the ENTER search must reset to 1.
STALE_PAGE_NUMBER = 3
FIRST_PAGE = 1
SEARCH_CURSOR_FIELD = "search_user_id"
# cbl/COUSR00C.cbl lines 251, 273, 637 and 212.
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid values are U and D"
# The frontend pages of XCTL COUSR02C and XCTL COUSR03C.
SELECTION_ROUTE_CASES = (
    pytest.param("U", "/admin/users/update?userId={userId}", id="update"),
    pytest.param("D", "/admin/users/delete?userId={userId}", id="delete"),
)
INVALID_SELECTION_CODE = "X"
# A seed key above the lowest one: with no users added, at most 10 user IDs are at or after it.
LATER_SEARCH_KEY = "ADMIN003"
# U and D transfer before the search key is read (cbl/COUSR00C.cbl lines 189-209), keyed with
# or without one.
SELECTION_SEARCH_KEYS = (
    pytest.param("", id="no-search"),
    pytest.param(LATER_SEARCH_KEY, id="with-search"),
)
# The list fields of a route answer: no page is browsed, so each keeps its default.
UNBROWSED_PAGE_FIELDS = {
    "rows": [],
    "page": 0,
    "first_key": "",
    "last_key": "",
    "message": "",
    "cursor_field": None,
}
# Why an F8 walk to the bottom literal fails, each reason followed by every page walked so far,
# so a paging regression fails with the keys it stalled on rather than paging forever.
WALK_BOUND_EXCEEDED_REASON = "No bottom literal within the pages the stored users fill"
WALK_STALLED_REASON = "An F8 page does not follow the last key walked"
WALK_REFUSED_REASON = "A page request of the walk was not answered with 200"
# The fields each walked page shows in that history.
WALK_HISTORY_FIELDS = ("page", "first_key", "last_key", "message")
# The scripted CU00 of the page-walk controls, which reads no database: 25 stored users fill
# three pages of 10.
CONTROL_BASE_URL = "http://cu00-control.invalid"
CONTROL_STORED_USER_COUNT = 25
CONTROL_KEY_TEMPLATE = "K{rowNumber:07d}"

# cbl/COUSR01C.cbl lines 120-144, in source order: (request field and cursor, literal).
REQUIRED_ADD_MESSAGES = (
    ("first_name", "First Name can NOT be empty..."),
    ("last_name", "Last Name can NOT be empty..."),
    ("user_id", "User ID can NOT be empty..."),
    ("password", "Password can NOT be empty..."),
    ("user_type", "User Type can NOT be empty..."),
)
REQUIRED_ADD_ORDER = tuple(fieldName for fieldName, _ in REQUIRED_ADD_MESSAGES)
# cbl/COUSR01C.cbl lines 255-257 and 263.
USER_ADDED_TEMPLATE = "User {userId} has been added ..."
USER_ID_EXISTS_MESSAGE = "User ID already exist..."
CREDENTIAL_EMPTY_MESSAGE = "Password can NOT be empty..."

# cbl/COUSR02C.cbl lines 182-206 without the password check (lines 198-203), which hashing
# makes unreachable: (blanked field and cursor, literal), in source order.
REQUIRED_UPDATE_MESSAGES = (
    ("first_name", "First Name can NOT be empty..."),
    ("last_name", "Last Name can NOT be empty..."),
    ("user_type", "User Type can NOT be empty..."),
)
REQUIRED_UPDATE_ORDER = tuple(fieldName for fieldName, _ in REQUIRED_UPDATE_MESSAGES)
# cbl/COUSR02C.cbl lines 148, 239, 336, 342 and 372-374.
USER_ID_EMPTY_MESSAGE = "User ID can NOT be empty..."
PLEASE_MODIFY_MESSAGE = "Please modify to update ..."
PRESS_PF5_TO_SAVE_MESSAGE = "Press PF5 key to save your updates ..."
USER_ID_NOT_FOUND_MESSAGE = "User ID NOT found..."
USER_UPDATED_TEMPLATE = "User {userId} has been updated ..."

# cbl/COUSR03C.cbl lines 283 and 318-320.
PRESS_PF5_TO_DELETE_MESSAGE = "Press PF5 key to delete this user ..."
USER_DELETED_TEMPLATE = "User {userId} has been deleted ..."

# cbl/COSGN00C.cbl sign-on literals, with the field each puts the cursor on.
SIGN_ON_USER_NOT_FOUND_MESSAGE = "User not found. Try again ..."
WRONG_CREDENTIAL_MESSAGE = "Wrong Password. Try again ..."
USER_ID_FIELD = "user_id"
CREDENTIAL_FIELD = "password"
FIRST_NAME_FIELD = "first_name"
# COMEN01C's admin-only literal, trailing space included, answers every role refusal.
ADMIN_ONLY_MESSAGE = "No access - Admin Only option... "
# The session refusal (401) and the CSRF origin refusal (403) carry an empty message.
NO_MESSAGE = ""
# Not allow-listed: OriginGuard matches the configured origin exactly, and localhost is a
# different site from 127.0.0.1.
FOREIGN_ORIGIN = "http://localhost:3000"
MISSING_ORIGIN = None
ORIGIN_HEADER = "Origin"

SELECT_USER_IDS_SQL = text("SELECT user_id FROM security.users")
SELECT_USER_SQL = text(
    "SELECT user_id, first_name, last_name, password_hash, user_type"
    " FROM security.users WHERE user_id = :userId"
)
SELECT_ALL_USERS_SQL = text(
    "SELECT user_id, first_name, last_name, password_hash, user_type"
    " FROM security.users ORDER BY user_id"
)


@dataclass(frozen=True)
class NewUser:
    """A COUSR01 add-screen entry, each field as keyed; repr hides the password."""

    userId: str
    firstName: str
    lastName: str
    password: str = field(repr=False)
    userType: str

    def RequestBody(self) -> dict[str, str]:
        """Return the UserCreateRequest JSON of this entry."""
        return {
            "first_name": self.firstName,
            "last_name": self.lastName,
            "user_id": self.userId,
            "password": self.password,
            "user_type": self.userType,
        }


@dataclass(frozen=True)
class UserAdminHarness:
    """The clients, engine and user factory one case needs, grouped to keep signatures short.

    ``anonymousClient`` signs users on; ``adminClient`` holds a type A session.
    """

    adminClient: httpx.AsyncClient
    anonymousClient: httpx.AsyncClient
    syncEngine: Engine
    createUser: Callable[[str], Any]


class Endpoint(NamedTuple):
    """One CU00-CU03 route: its method and URL; the guard cases send each one."""

    method: str
    url: str


SEED_USER_URL = f"{USERS_URL}/{SEED_REGULAR_USER_ID}"
LIST_ENDPOINT = Endpoint("GET", USERS_URL)
SELECTION_ENDPOINT = Endpoint("POST", SELECTION_URL)
ADD_ENDPOINT = Endpoint("POST", USERS_URL)
FETCH_ENDPOINT = Endpoint("GET", SEED_USER_URL)
UPDATE_ENDPOINT = Endpoint("PUT", SEED_USER_URL)
DELETE_ENDPOINT = Endpoint("DELETE", SEED_USER_URL)
ALL_ENDPOINTS = (
    pytest.param(LIST_ENDPOINT, id="cu00-list"),
    pytest.param(SELECTION_ENDPOINT, id="cu00-selection"),
    pytest.param(ADD_ENDPOINT, id="cu01-add"),
    pytest.param(FETCH_ENDPOINT, id="cu02-cu03-fetch"),
    pytest.param(UPDATE_ENDPOINT, id="cu02-update"),
    pytest.param(DELETE_ENDPOINT, id="cu03-delete"),
)
WRITE_ENDPOINTS = (
    pytest.param(ADD_ENDPOINT, id="cu01-add"),
    pytest.param(UPDATE_ENDPOINT, id="cu02-update"),
    pytest.param(DELETE_ENDPOINT, id="cu03-delete"),
)
REFUSED_ORIGINS = (
    pytest.param(MISSING_ORIGIN, id="no-origin"),
    pytest.param(FOREIGN_ORIGIN, id="foreign-origin"),
)


class SearchedInvalidSelection(NamedTuple):
    """An invalid code keyed with a search key, the users added first, and the answer.

    ``outcomeFields`` are the body fields beside the page and its literal: the 400's other
    legacy error keys, or the 200's empty route.
    """

    addedUserIds: tuple[str, ...]
    searchUserId: str
    expectedStatus: int
    expectedMessage: str
    outcomeFields: Mapping[str, Any]


# With MIXED_KEYS added, a1 is the lowest key and more than 10 user IDs are at or after ADMIN001:
# the look-ahead READNEXT finds a row and the invalid-selection literal stays (cbl/COUSR00C.cbl
# line 212). With none added, READNEXT ENDFILE after LATER_SEARCH_KEY moves the bottom literal
# over it (line 637).
SEARCHED_INVALID_SELECTION_CASES = (
    pytest.param(
        SearchedInvalidSelection(
            MIXED_KEYS,
            SEED_ADMIN_USER_ID,
            HTTP_EDIT_FAILURE,
            INVALID_SELECTION_MESSAGE,
            {"info_message": "", "field_errors": []},
        ),
        id="next-page-keeps-the-literal",
    ),
    pytest.param(
        SearchedInvalidSelection(
            (), LATER_SEARCH_KEY, HTTP_OK, REACHED_BOTTOM_MESSAGE, {"route": None}
        ),
        id="end-of-file-replaces-the-literal",
    ),
)


def NewUserId() -> str:
    """Return a generated 7-character upper-case user ID that no seed or fixture user has."""
    return GENERATED_USER_ID_PREFIX + secrets.token_hex(USER_ID_HEX_BYTES).upper()


def NewPassword() -> str:
    """Return a generated 8-character upper-case password, which signs on as created.

    COSGN00C upper-cases sign-on input, so only an upper-case password can match its hash.
    """
    return secrets.token_hex(PASSWORD_HEX_BYTES).upper()


def NewLowerCasePassword() -> str:
    """Return a generated 8-character password that holds at least one lower-case letter.

    A lower-cased hexadecimal token may be all digits, which upper-casing leaves unchanged, so
    the first character is a random lower-case letter: the password then never equals its
    upper-cased sign-on form.
    """
    return secrets.choice(string.ascii_lowercase) + secrets.token_hex(PASSWORD_HEX_BYTES)[1:]


def NewUserEntry(userId: str, userType: str = REGULAR_USER_TYPE, password: str = "") -> NewUser:
    """Return a complete add-screen entry for ``userId``; a blank ``password`` is generated."""
    return NewUser(
        userId=userId,
        firstName=ADDED_FIRST_NAME,
        lastName=ADDED_LAST_NAME,
        password=password or NewPassword(),
        userType=userType,
    )


def EbcdicOrder(userIds: Iterable[str]) -> list[str]:
    """Return ``userIds`` in code-page-037 byte order, the VSAM key order of USRSEC."""
    return sorted(userIds, key=lambda value: value.encode(EBCDIC_CODEC))


def UserIdsFrom(orderedIds: Iterable[str], searchUserId: str) -> list[str]:
    """Return the ``orderedIds`` at or after ``searchUserId`` in code-page-037 order: GTEQ."""
    searchKey = searchUserId.encode(EBCDIC_CODEC)
    return [userId for userId in orderedIds if userId.encode(EBCDIC_CODEC) >= searchKey]


def ReadStoredUserIds(syncEngine: Engine) -> list[str]:
    """Return every user ID in security.users, in no particular order."""
    with syncEngine.connect() as connection:
        return list(connection.execute(SELECT_USER_IDS_SQL).scalars())


def ReadStoredUser(syncEngine: Engine, userId: str) -> dict[str, Any] | None:
    """Return the security.users row of ``userId`` as a column-name dict, or None if absent."""
    with syncEngine.connect() as connection:
        storedRow = connection.execute(SELECT_USER_SQL, {"userId": userId}).mappings().first()
    return dict(storedRow) if storedRow is not None else None


def ReadAllStoredUsers(syncEngine: Engine) -> list[tuple[Any, ...]]:
    """Return every security.users row, so a refused request can be shown to change nothing."""
    with syncEngine.connect() as connection:
        return [tuple(storedRow) for storedRow in connection.execute(SELECT_ALL_USERS_SQL)]


def AssertLegacyError(
    response: httpx.Response, status: int, message: str, cursorField: str = ""
) -> None:
    """Assert a legacy error body: the status, the verbatim literal and the cursor field."""
    assert response.status_code == status, response.text
    body = response.json()
    assert LEGACY_ERROR_KEYS <= body.keys(), body
    assert body["message"] == message
    assert body["cursor_field"] == cursorField


def AssertMessage(response: httpx.Response, message: str, cursorField: str) -> None:
    """Assert a 200 UserMessageResponse carrying exactly ``message`` and ``cursorField``."""
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {"message": message, "cursor_field": cursorField}


def AssertAdded(response: httpx.Response, userId: str) -> None:
    """Assert COUSR01C's added literal, with the cursor back on the first name."""
    AssertMessage(response, USER_ADDED_TEMPLATE.format(userId=userId), FIRST_NAME_FIELD)


def JsonKeys(value: Any) -> Iterator[str]:
    """Yield every object key of a decoded JSON value, at any depth."""
    if isinstance(value, Mapping):
        for keyName, nestedValue in value.items():
            yield keyName
            yield from JsonKeys(nestedValue)
    elif isinstance(value, list):
        for nestedValue in value:
            yield from JsonKeys(nestedValue)


def AssertNoSecrets(response: httpx.Response, plaintext: str, storedHash: str) -> None:
    """Assert ``response`` carries neither the password nor its hash, nor a key naming either."""
    assert plaintext and storedHash, "both secrets must be non-blank to be searched for"
    assert plaintext not in response.text
    assert storedHash not in response.text
    for keyName in JsonKeys(response.json()):
        assert not any(fragment in keyName.lower() for fragment in SECRET_KEY_FRAGMENTS), keyName


def AssertSearchedPage(
    body: Mapping[str, Any], case: SearchedInvalidSelection, pageIds: list[str]
) -> None:
    """Assert page 1 browsed from the search key: every body field, and its rows' user IDs."""
    expectedFields = {
        "message": case.expectedMessage,
        "cursor_field": SEARCH_CURSOR_FIELD,
        "page": FIRST_PAGE,
        "first_key": pageIds[0],
        "last_key": pageIds[-1],
        **case.outcomeFields,
    }
    assert {fieldName: value for fieldName, value in body.items() if fieldName != "rows"} == (
        expectedFields
    )
    assert [row["user_id"] for row in body["rows"]] == pageIds


async def AddUser(client: httpx.AsyncClient, request: NewUser) -> httpx.Response:
    """Send ``request`` to CU01's ENTER, POST /admin/users."""
    return await client.post(USERS_URL, json=request.RequestBody())


async def FetchUser(
    client: httpx.AsyncClient, userId: str, screen: UserFetchScreen
) -> httpx.Response:
    """Send ENTER on COUSR02 or COUSR03, GET /admin/users/{userId}, for ``screen``."""
    return await client.get(f"{USERS_URL}/{userId}", params={"screen": screen.value})


async def UpdateUser(
    client: httpx.AsyncClient, userId: str, body: Mapping[str, str]
) -> httpx.Response:
    """Send F5 on COUSR02, PUT /admin/users/{userId}, with ``body``."""
    return await client.put(f"{USERS_URL}/{userId}", json=dict(body))


async def DeleteUser(client: httpx.AsyncClient, userId: str) -> httpx.Response:
    """Send F5 on COUSR03, DELETE /admin/users/{userId}."""
    return await client.delete(f"{USERS_URL}/{userId}")


async def SignOn(client: httpx.AsyncClient, userId: str, password: str) -> httpx.Response:
    """Sign on through CC00, POST /auth/login, with the credentials as typed."""
    return await client.post(LOGIN_URL, json={"user_id": userId, "password": password})


def UpdateBody(
    storedUser: Mapping[str, Any], changes: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return a COUSR02 F5 body showing ``storedUser`` as fetched, blank password, plus changes."""
    body = {
        "first_name": storedUser["first_name"],
        "last_name": storedUser["last_name"],
        "password": "",
        "user_type": storedUser["user_type"],
    }
    body.update(changes or {})
    return body


def BlankedFrom(body: Mapping[str, str], order: tuple[str, ...], firstBlank: str) -> dict[str, str]:
    """Return ``body`` with ``firstBlank`` and every field after it in ``order`` blanked."""
    blankFields = order[order.index(firstBlank) :]
    return {
        fieldName: "" if fieldName in blankFields else value for fieldName, value in body.items()
    }


def SelectionBody(userId: str, selectionCode: str, searchUserId: str = "") -> dict[str, Any]:
    """Return a COUSR00 ENTER body with one row selected and ``searchUserId``, blank by default."""
    return {
        "selections": [{"user_id": userId, "selection": selectionCode}],
        "search_user_id": searchUserId,
    }


async def PressPagingKey(
    client: httpx.AsyncClient, shownPage: Mapping[str, Any], direction: str
) -> httpx.Response:
    """Press F8 (forward, after last_key) or F7 (backward, before first_key) on ``shownPage``."""
    keyName = "last_key" if direction == FORWARD_DIRECTION else "first_key"
    query = {"direction": direction, "startKey": shownPage[keyName], "page": shownPage["page"]}
    return await client.get(USERS_URL, params=query)


def MaxPageCount(storedUserCount: int) -> int:
    """Return the pages of 10 that ``storedUserCount`` rows fill; ENTER always shows one page."""
    return max(FIRST_PAGE, (storedUserCount + PAGE_SIZE - 1) // PAGE_SIZE)


def WalkHistory(pages: Sequence[Mapping[str, Any]]) -> str:
    """Return one numbered line per walked page: its page number, first and last keys, literal."""
    historyLines = [
        f"  {position}: "
        + ", ".join(f"{fieldName}={listPage.get(fieldName)!r}" for fieldName in WALK_HISTORY_FIELDS)
        for position, listPage in enumerate(pages, start=1)
    ]
    return "\n".join(historyLines) or "  none"


def FailWalk(reason: str, detail: str, pages: Sequence[Mapping[str, Any]]) -> NoReturn:
    """Fail the CU00 walk with ``reason``, ``detail`` and every page it read, in order."""
    pytest.fail(f"{reason}: {detail}\nPages walked so far:\n{WalkHistory(pages)}")


def ReadListPage(response: httpx.Response, pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the COUSR00 page ``response`` carries; fail the walk of ``pages`` unless a 200."""
    if response.status_code != HTTP_OK:
        detail = f"HTTP {response.status_code} {response.text}"
        FailWalk(WALK_REFUSED_REASON, detail, pages)
    return response.json()


def AssertAdvances(pages: Sequence[Mapping[str, Any]], nextPage: Mapping[str, Any]) -> None:
    """Fail at once unless both keys of ``nextPage`` sort after the last key walked, in EBCDIC.

    F8 browses exclusively after last_key, so the last keys of a sound walk strictly increase:
    a page that repeats a key, or does not get past the last one, is a walk that never ends.
    """
    lastKey = pages[-1]["last_key"]
    pageKeys = (nextPage["first_key"], nextPage["last_key"])
    lastKeyBytes = lastKey.encode(EBCDIC_CODEC)
    if not all(pageKey.encode(EBCDIC_CODEC) > lastKeyBytes for pageKey in pageKeys):
        detail = f"keys {pageKeys!r} after last_key {lastKey!r}"
        FailWalk(WALK_STALLED_REASON, detail, [*pages, nextPage])


async def ReadAllPages(client: httpx.AsyncClient, storedUserCount: int) -> list[dict[str, Any]]:
    """Return every COUSR00 page: ENTER from the lowest key, then F8 until the bottom literal.

    The last page always carries ``You have reached the bottom of the page...``: the browse
    reads one row beyond the page, so a full last page reports the end of file as well. The
    walk is bounded by the pages ``storedUserCount`` rows fill, counted from the database by the
    caller, and every F8 page must get past the last key: a refused, stalled or overlong walk
    fails with its page history instead of paging forever.
    """
    maxPages = MaxPageCount(storedUserCount)
    pages = [ReadListPage(await client.get(USERS_URL), [])]
    while pages[-1]["message"] != REACHED_BOTTOM_MESSAGE:
        if len(pages) >= maxPages:
            detail = f"{maxPages} pages of {PAGE_SIZE} for {storedUserCount} stored users"
            FailWalk(WALK_BOUND_EXCEEDED_REASON, detail, pages)
        nextResponse = await PressPagingKey(client, pages[-1], FORWARD_DIRECTION)
        nextPage = ReadListPage(nextResponse, pages)
        AssertAdvances(pages, nextPage)
        pages.append(nextPage)
    return pages


@dataclass
class ScriptedUserList:
    """A MockTransport handler answering request n with ``answer(n)``, counting the requests."""

    answer: Callable[[int], httpx.Response]
    requestCount: int = 0

    def Handle(self, request: httpx.Request) -> httpx.Response:
        """Count ``request`` and answer it as the script's next response."""
        self.requestCount += 1
        return self.answer(self.requestCount)


def ControlPage(pageNumber: int) -> dict[str, Any]:
    """Return full scripted page ``pageNumber``, rows after every earlier page's, no literal."""
    firstRow = (pageNumber - 1) * PAGE_SIZE + 1
    rowIds = [
        CONTROL_KEY_TEMPLATE.format(rowNumber=rowNumber)
        for rowNumber in range(firstRow, firstRow + PAGE_SIZE)
    ]
    return {
        "rows": [{"user_id": rowId} for rowId in rowIds],
        "page": pageNumber,
        "first_key": rowIds[0],
        "last_key": rowIds[-1],
        "message": NO_MESSAGE,
        "cursor_field": SEARCH_CURSOR_FIELD,
    }


def RepeatedPageAnswer(requestNumber: int) -> httpx.Response:
    """Answer every request with the same full first page: an F8 that never moves on."""
    return httpx.Response(HTTP_OK, json=ControlPage(FIRST_PAGE))


def EndlessPageAnswer(requestNumber: int) -> httpx.Response:
    """Answer request n with full page n: the keys advance, but the bottom literal never comes."""
    return httpx.Response(HTTP_OK, json=ControlPage(requestNumber))


def RefusedForwardAnswer(requestNumber: int) -> httpx.Response:
    """Answer ENTER with a full first page and every F8 with the 400 bottom refusal."""
    if requestNumber == FIRST_PAGE:
        return EndlessPageAnswer(requestNumber)
    refusalBody = {
        "message": ALREADY_AT_BOTTOM_MESSAGE,
        "info_message": "",
        "field_errors": [],
        "cursor_field": SEARCH_CURSOR_FIELD,
    }
    return httpx.Response(HTTP_EDIT_FAILURE, json=refusalBody)


class WalkFailureCase(NamedTuple):
    """A scripted CU00 answer and how ReadAllPages must fail on it.

    ``requestCount`` is the requests sent before the failure; ``historyLength`` the leading
    scripted 200 pages its history lists.
    """

    answer: Callable[[int], httpx.Response]
    reason: str
    requestCount: int
    historyLength: int


# CONTROL_STORED_USER_COUNT bounds the endless walk at three pages; the repeated page fails on
# the first F8, and the refusal on the first F8 lists only page 1.
WALK_FAILURE_CASES = (
    pytest.param(WalkFailureCase(RepeatedPageAnswer, WALK_STALLED_REASON, 2, 2), id="same-page"),
    pytest.param(
        WalkFailureCase(EndlessPageAnswer, WALK_BOUND_EXCEEDED_REASON, 3, 3), id="never-bottom"
    ),
    pytest.param(WalkFailureCase(RefusedForwardAnswer, WALK_REFUSED_REASON, 2, 1), id="refused"),
)


def EndpointBody(endpoint: Endpoint) -> dict[str, Any] | None:
    """Return a body that would change USRSEC if ``endpoint`` accepted it; None for GET and DELETE.

    The guard cases send it, so a refusal is shown to have changed nothing a success would.
    """
    if endpoint == ADD_ENDPOINT:
        return NewUserEntry(NewUserId()).RequestBody()
    if endpoint == SELECTION_ENDPOINT:
        return SelectionBody(SEED_REGULAR_USER_ID, "U")
    if endpoint == UPDATE_ENDPOINT:
        changedUser = {
            "first_name": GUARDED_NAME,
            "last_name": GUARDED_NAME,
            "user_type": REGULAR_USER_TYPE,
        }
        return UpdateBody(changedUser, {"password": NewPassword()})
    return None


async def SendWithOrigin(
    client: httpx.AsyncClient, endpoint: Endpoint, origin: str | None
) -> httpx.Response:
    """Send ``endpoint`` with its EndpointBody, replacing the client's Origin or dropping it."""
    request = client.build_request(endpoint.method, endpoint.url, json=EndpointBody(endpoint))
    if origin is None:
        del request.headers[ORIGIN_HEADER]
    else:
        request.headers[ORIGIN_HEADER] = origin
    return await client.send(request)


@pytest.fixture(name="harness")
def HarnessFixture(
    adminClient: httpx.AsyncClient,
    apiClient: httpx.AsyncClient,
    syncEngine: Engine,
    createUser: Callable[[str], Any],
) -> UserAdminHarness:
    """Return the admin and anonymous clients of one app, the engine and the user factory."""
    return UserAdminHarness(
        adminClient=adminClient,
        anonymousClient=apiClient,
        syncEngine=syncEngine,
        createUser=createUser,
    )


async def test_cu00_pages_list_every_user_in_ebcdic_key_order(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Pages of at most 10 rows list every user in EBCDIC order: a1, AA, ADMIN001, A1, 1A."""
    for userId in MIXED_KEYS:
        AssertAdded(await AddUser(adminClient, NewUserEntry(userId)), userId)
    storedIds = ReadStoredUserIds(syncEngine)
    pages = await ReadAllPages(adminClient, len(storedIds))
    listedIds = [row["user_id"] for listPage in pages for row in listPage["rows"]]
    assert all(len(listPage["rows"]) <= PAGE_SIZE for listPage in pages)
    assert [listPage["page"] for listPage in pages] == list(range(FIRST_PAGE, len(pages) + 1))
    assert listedIds == EbcdicOrder(storedIds)
    mixedOrder = [userId for userId in listedIds if userId in EXPECTED_MIXED_ORDER]
    assert mixedOrder == list(EXPECTED_MIXED_ORDER)
    assert sorted(EXPECTED_MIXED_ORDER) != mixedOrder


async def test_cu00_paging_keys_refuse_past_either_end(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """F7 on page 1 and F8 after the last page are refused with 400 and their literals.

    COUSR00C only moves the cursor to USRIDIN, so neither refusal lists a failing field.
    """
    pages = await ReadAllPages(adminClient, len(ReadStoredUserIds(syncEngine)))
    topResponse = await PressPagingKey(adminClient, pages[0], BACKWARD_DIRECTION)
    AssertLegacyError(topResponse, HTTP_EDIT_FAILURE, ALREADY_AT_TOP_MESSAGE, SEARCH_CURSOR_FIELD)
    bottomResponse = await PressPagingKey(adminClient, pages[-1], FORWARD_DIRECTION)
    AssertLegacyError(
        bottomResponse, HTTP_EDIT_FAILURE, ALREADY_AT_BOTTOM_MESSAGE, SEARCH_CURSOR_FIELD
    )
    assert [topResponse.json()["field_errors"], bottomResponse.json()["field_errors"]] == [[], []]


@pytest.mark.parametrize("case", WALK_FAILURE_CASES)
async def test_cu00_page_walk_fails_with_its_history_instead_of_paging_forever(
    case: WalkFailureCase,
) -> None:
    """A stalled, endless or refused F8 walk fails at once, listing every page it read.

    The scripted CU00 answers through httpx.MockTransport, so no database is read, and the
    request count shows the walk stopped where it should.
    """
    scriptedList = ScriptedUserList(case.answer)
    transport = httpx.MockTransport(scriptedList.Handle)
    async with httpx.AsyncClient(transport=transport, base_url=CONTROL_BASE_URL) as client:
        with pytest.raises(pytest.fail.Exception) as walkFailure:
            await ReadAllPages(client, CONTROL_STORED_USER_COUNT)
    failureText = str(walkFailure.value)
    scriptedPages = [
        case.answer(requestNumber).json()
        for requestNumber in range(FIRST_PAGE, case.historyLength + 1)
    ]
    assert failureText.startswith(case.reason), failureText
    assert failureText.endswith(f"Pages walked so far:\n{WalkHistory(scriptedPages)}")
    assert scriptedList.requestCount == case.requestCount


@pytest.mark.parametrize(("searchUserId", "expectedFirstId"), SEARCH_CASES)
async def test_cu00_search_starts_at_first_key_at_or_after_it(
    adminClient: httpx.AsyncClient, searchUserId: str, expectedFirstId: str
) -> None:
    """ENTER browses from the search key inclusively (STARTBR GTEQ) and resets to page 1."""
    query = {"searchUserId": searchUserId, "page": STALE_PAGE_NUMBER}
    response = await adminClient.get(USERS_URL, params=query)
    assert response.status_code == HTTP_OK, response.text
    listPage = response.json()
    assert listPage["page"] == FIRST_PAGE
    assert listPage["rows"][0]["user_id"] == expectedFirstId
    assert listPage["first_key"] == expectedFirstId


async def test_cu00_blank_search_starts_at_lowest_key(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A blank search key is LOW-VALUES: the page starts at the lowest user ID in EBCDIC order."""
    response = await adminClient.get(USERS_URL, params={"searchUserId": " "})
    assert response.status_code == HTTP_OK, response.text
    listPage = response.json()
    expectedIds = EbcdicOrder(ReadStoredUserIds(syncEngine))[:PAGE_SIZE]
    assert [row["user_id"] for row in listPage["rows"]] == expectedIds
    assert listPage["page"] == FIRST_PAGE


@pytest.mark.parametrize("searchUserId", SELECTION_SEARCH_KEYS)
@pytest.mark.parametrize(("selectionCode", "routeTemplate"), SELECTION_ROUTE_CASES)
async def test_cu00_selection_routes_to_update_or_delete(
    harness: UserAdminHarness, selectionCode: str, routeTemplate: str, searchUserId: str
) -> None:
    """U routes to the COUSR02C page and D to the COUSR03C page, keyed by the selected ID.

    The selection is processed before the search key and its XCTL ends the task, so a keyed
    search key is never browsed: the answer is the route with no page, and USRSEC is unchanged.
    """
    usersBefore = ReadAllStoredUsers(harness.syncEngine)
    selectionBody = SelectionBody(SEED_REGULAR_USER_ID, selectionCode, searchUserId)
    response = await harness.adminClient.post(SELECTION_URL, json=selectionBody)
    assert response.status_code == HTTP_OK, response.text
    expectedRoute = routeTemplate.format(userId=SEED_REGULAR_USER_ID)
    assert response.json() == {"route": expectedRoute, **UNBROWSED_PAGE_FIELDS}
    assert ReadAllStoredUsers(harness.syncEngine) == usersBefore


async def test_cu00_invalid_selection_code_is_refused_with_its_page(
    adminClient: httpx.AsyncClient,
) -> None:
    """Any other code is 400 ``Invalid selection. Valid values are U and D`` with page 1.

    The ten seed users and the admin client's user fill more than one page, so the browse that
    follows the selection reports no end of file and leaves the literal on the message line.
    """
    selectionBody = SelectionBody(SEED_REGULAR_USER_ID, INVALID_SELECTION_CODE)
    response = await adminClient.post(SELECTION_URL, json=selectionBody)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, INVALID_SELECTION_MESSAGE, SEARCH_CURSOR_FIELD)
    refusalBody = response.json()
    assert refusalBody["page"] == FIRST_PAGE
    assert len(refusalBody["rows"]) == PAGE_SIZE
    assert "route" not in refusalBody


@pytest.mark.parametrize("case", SEARCHED_INVALID_SELECTION_CASES)
async def test_cu00_invalid_selection_with_a_search_browses_from_the_search_key(
    harness: UserAdminHarness, case: SearchedInvalidSelection
) -> None:
    """Any other code with a search key browses page 1 from that key, inclusive.

    The search key, not the lowest key, is moved to SEC-USR-ID after the selection
    (cbl/COUSR00C.cbl lines 218-228). The literal stays on a 400 while a next page exists, and
    gives way to the bottom literal on a 200 at end of file. USRSEC is unchanged either way.
    """
    for userId in case.addedUserIds:
        AssertAdded(await AddUser(harness.adminClient, NewUserEntry(userId)), userId)
    storedOrder = EbcdicOrder(ReadStoredUserIds(harness.syncEngine))
    keyedIds = UserIdsFrom(storedOrder, case.searchUserId)
    usersBefore = ReadAllStoredUsers(harness.syncEngine)
    selectionBody = SelectionBody(SEED_REGULAR_USER_ID, INVALID_SELECTION_CODE, case.searchUserId)
    response = await harness.adminClient.post(SELECTION_URL, json=selectionBody)
    assert ReadAllStoredUsers(harness.syncEngine) == usersBefore
    hasNextPage = len(keyedIds) > PAGE_SIZE
    assert hasNextPage == (case.expectedStatus == HTTP_EDIT_FAILURE)
    assert keyedIds[0] == case.searchUserId
    assert storedOrder[0] != case.searchUserId
    assert response.status_code == case.expectedStatus, response.text
    AssertSearchedPage(response.json(), case, keyedIds[:PAGE_SIZE])


@pytest.mark.parametrize(("blankField", "expectedMessage"), REQUIRED_ADD_MESSAGES)
async def test_cu01_first_blank_field_in_source_order_ends_validation(
    adminClient: httpx.AsyncClient, syncEngine: Engine, blankField: str, expectedMessage: str
) -> None:
    """Blanking a field and every later one answers that field's literal: first failure wins."""
    userIdsBefore = sorted(ReadStoredUserIds(syncEngine))
    entryBody = NewUserEntry(NewUserId()).RequestBody()
    response = await adminClient.post(
        USERS_URL, json=BlankedFrom(entryBody, REQUIRED_ADD_ORDER, blankField)
    )
    AssertLegacyError(response, HTTP_EDIT_FAILURE, expectedMessage, blankField)
    assert sorted(ReadStoredUserIds(syncEngine)) == userIdsBefore


@pytest.mark.parametrize("userType", UNRESTRICTED_USER_TYPES)
async def test_cu01_any_user_type_is_stored_as_entered(
    adminClient: httpx.AsyncClient, syncEngine: Engine, userType: str
) -> None:
    """COUSR01C checks no user-type value: Z and lower-case u are added and kept as keyed."""
    entry = NewUserEntry(NewUserId(), userType)
    AssertAdded(await AddUser(adminClient, entry), entry.userId)
    storedUser = ReadStoredUser(syncEngine, entry.userId)
    assert storedUser is not None
    assert storedUser["user_type"] == userType
    assert storedUser["first_name"] == entry.firstName
    assert storedUser["last_name"] == entry.lastName


async def test_cu01_lower_case_password_is_hashed_as_entered_and_never_signs_on(
    harness: UserAdminHarness,
) -> None:
    """The hash verifies the password as keyed; COSGN00C upper-cases it, so sign-on fails."""
    entry = NewUserEntry(NewUserId(), password=NewLowerCasePassword())
    AssertAdded(await AddUser(harness.adminClient, entry), entry.userId)
    storedUser = ReadStoredUser(harness.syncEngine, entry.userId)
    assert storedUser is not None
    assert VerifyPassword(entry.password, storedUser["password_hash"])
    assert not VerifyPassword(entry.password.upper(), storedUser["password_hash"])
    signOnResponse = await SignOn(harness.anonymousClient, entry.userId, entry.password)
    AssertLegacyError(
        signOnResponse, HTTP_SIGN_ON_FAILURE, WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD
    )


async def test_cu01_upper_case_password_signs_on(harness: UserAdminHarness) -> None:
    entry = NewUserEntry(NewUserId())
    AssertAdded(await AddUser(harness.adminClient, entry), entry.userId)
    signOnResponse = await SignOn(harness.anonymousClient, entry.userId, entry.password)
    assert signOnResponse.status_code == HTTP_OK, signOnResponse.text
    assert signOnResponse.json()["user_id"] == entry.userId


async def test_cu01_existing_user_id_is_refused(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """Adding an existing key is the DUPKEY branch: 409 ``User ID already exist...``."""
    storedBefore = ReadStoredUser(syncEngine, SEED_REGULAR_USER_ID)
    response = await AddUser(adminClient, NewUserEntry(SEED_REGULAR_USER_ID))
    AssertLegacyError(response, HTTP_DUPLICATE_KEY, USER_ID_EXISTS_MESSAGE, USER_ID_FIELD)
    assert ReadStoredUser(syncEngine, SEED_REGULAR_USER_ID) == storedBefore


async def test_cu01_user_id_beyond_bms_length_is_malformed(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """A 9-character user ID exceeds USERID LENGTH=8, which no 3270 can send: 422."""
    overlongUserId = NewUserId().ljust(OVERLONG_USER_ID_LENGTH, OVERLONG_PADDING)
    response = await AddUser(adminClient, NewUserEntry(overlongUserId))
    assert response.status_code == HTTP_MALFORMED, response.text
    assert ReadStoredUser(syncEngine, overlongUserId) is None


async def test_cu02_fetch_shows_the_user_with_the_save_prompt(harness: UserAdminHarness) -> None:
    """ENTER on COUSR02 shows the stored fields and the PF5 prompt, never a password or hash."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedUser = ReadStoredUser(harness.syncEngine, user.userId)
    response = await FetchUser(harness.adminClient, user.userId, UserFetchScreen.UPDATE)
    assert response.status_code == HTTP_OK, response.text
    assert response.json() == {
        "user_id": user.userId,
        "first_name": storedUser["first_name"],
        "last_name": storedUser["last_name"],
        "user_type": REGULAR_USER_TYPE,
        "message": PRESS_PF5_TO_SAVE_MESSAGE,
    }
    AssertNoSecrets(response, user.password, storedUser["password_hash"])


@pytest.mark.parametrize("screen", list(UserFetchScreen))
async def test_fetch_refuses_blank_and_unknown_user_ids(
    adminClient: httpx.AsyncClient, screen: UserFetchScreen
) -> None:
    """Both fetches answer 400 for a blank ID and 404 for an absent one, cursor on the ID."""
    blankResponse = await FetchUser(adminClient, BLANK_USER_ID_SEGMENT, screen)
    AssertLegacyError(blankResponse, HTTP_EDIT_FAILURE, USER_ID_EMPTY_MESSAGE, USER_ID_FIELD)
    unknownResponse = await FetchUser(adminClient, NewUserId(), screen)
    AssertLegacyError(unknownResponse, HTTP_NOT_FOUND, USER_ID_NOT_FOUND_MESSAGE, USER_ID_FIELD)


async def test_cu02_name_only_update_keeps_the_password_hash(harness: UserAdminHarness) -> None:
    """A blank password means unchanged: the name is rewritten and the old password signs on."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedBefore = ReadStoredUser(harness.syncEngine, user.userId)
    updateBody = UpdateBody(storedBefore, {"first_name": RENAMED_FIRST_NAME})
    response = await UpdateUser(harness.adminClient, user.userId, updateBody)
    AssertMessage(response, USER_UPDATED_TEMPLATE.format(userId=user.userId), FIRST_NAME_FIELD)
    storedAfter = ReadStoredUser(harness.syncEngine, user.userId)
    assert storedAfter["first_name"] == RENAMED_FIRST_NAME
    assert storedAfter["password_hash"] == storedBefore["password_hash"]
    signOnResponse = await SignOn(harness.anonymousClient, user.userId, user.password)
    assert signOnResponse.status_code == HTTP_OK, signOnResponse.text


@pytest.mark.parametrize(
    "reenterCurrentPassword",
    [pytest.param(False, id="blank-password"), pytest.param(True, id="current-password")],
)
async def test_cu02_unchanged_submission_asks_for_a_modification(
    harness: UserAdminHarness, reenterCurrentPassword: bool
) -> None:
    """Nothing changed, whether the password is blank or re-entered: 400 and the hash is kept."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedBefore = ReadStoredUser(harness.syncEngine, user.userId)
    enteredPassword = user.password if reenterCurrentPassword else ""
    updateBody = UpdateBody(storedBefore, {"password": enteredPassword})
    response = await UpdateUser(harness.adminClient, user.userId, updateBody)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, PLEASE_MODIFY_MESSAGE, FIRST_NAME_FIELD)
    assert response.json()["field_errors"] == []
    assert response.json()["message"] != CREDENTIAL_EMPTY_MESSAGE
    assert ReadStoredUser(harness.syncEngine, user.userId) == storedBefore


async def test_cu02_new_password_replaces_the_hash(harness: UserAdminHarness) -> None:
    """A password that does not verify replaces the hash; only the new password signs on."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedBefore = ReadStoredUser(harness.syncEngine, user.userId)
    newPassword = NewPassword()
    updateBody = UpdateBody(storedBefore, {"password": newPassword})
    response = await UpdateUser(harness.adminClient, user.userId, updateBody)
    AssertMessage(response, USER_UPDATED_TEMPLATE.format(userId=user.userId), FIRST_NAME_FIELD)
    newHash = ReadStoredUser(harness.syncEngine, user.userId)["password_hash"]
    assert newHash != storedBefore["password_hash"]
    assert VerifyPassword(newPassword, newHash)
    newSignOn = await SignOn(harness.anonymousClient, user.userId, newPassword)
    assert newSignOn.status_code == HTTP_OK, newSignOn.text
    oldSignOn = await SignOn(harness.anonymousClient, user.userId, user.password)
    AssertLegacyError(oldSignOn, HTTP_SIGN_ON_FAILURE, WRONG_CREDENTIAL_MESSAGE, CREDENTIAL_FIELD)


async def test_cu02_responses_never_expose_passwords_or_hashes(harness: UserAdminHarness) -> None:
    user = harness.createUser(REGULAR_USER_TYPE)
    storedBefore = ReadStoredUser(harness.syncEngine, user.userId)
    newPassword = NewPassword()
    replacementBody = UpdateBody(storedBefore, {"password": newPassword})
    responses = [
        await FetchUser(harness.adminClient, user.userId, UserFetchScreen.UPDATE),
        await UpdateUser(harness.adminClient, user.userId, replacementBody),
        await UpdateUser(harness.adminClient, user.userId, replacementBody),
        await FetchUser(harness.adminClient, user.userId, UserFetchScreen.UPDATE),
    ]
    newHash = ReadStoredUser(harness.syncEngine, user.userId)["password_hash"]
    assert [response.status_code for response in responses] == [
        HTTP_OK,
        HTTP_OK,
        HTTP_EDIT_FAILURE,
        HTTP_OK,
    ]
    for response in responses:
        AssertNoSecrets(response, user.password, storedBefore["password_hash"])
        AssertNoSecrets(response, newPassword, newHash)


@pytest.mark.parametrize(("blankField", "expectedMessage"), REQUIRED_UPDATE_MESSAGES)
async def test_cu02_presence_checks_run_in_source_order(
    harness: UserAdminHarness, blankField: str, expectedMessage: str
) -> None:
    """Blanking a field and every later one answers that field's literal and writes nothing."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedBefore = ReadStoredUser(harness.syncEngine, user.userId)
    updateBody = UpdateBody(storedBefore, {"first_name": RENAMED_FIRST_NAME})
    blankedBody = BlankedFrom(updateBody, REQUIRED_UPDATE_ORDER, blankField)
    response = await UpdateUser(harness.adminClient, user.userId, blankedBody)
    AssertLegacyError(response, HTTP_EDIT_FAILURE, expectedMessage, blankField)
    assert ReadStoredUser(harness.syncEngine, user.userId) == storedBefore


async def test_cu02_update_refuses_blank_and_unknown_user_ids(
    adminClient: httpx.AsyncClient, syncEngine: Engine
) -> None:
    """PUT answers 400 for a blank ID and 404 for an absent one, and writes nothing."""
    usersBefore = ReadAllStoredUsers(syncEngine)
    changes = {"first_name": RENAMED_FIRST_NAME, "last_name": ADDED_LAST_NAME}
    updateBody = UpdateBody(changes | {"user_type": REGULAR_USER_TYPE})
    blankResponse = await UpdateUser(adminClient, BLANK_USER_ID_SEGMENT, updateBody)
    AssertLegacyError(blankResponse, HTTP_EDIT_FAILURE, USER_ID_EMPTY_MESSAGE, USER_ID_FIELD)
    unknownResponse = await UpdateUser(adminClient, NewUserId(), updateBody)
    AssertLegacyError(unknownResponse, HTTP_NOT_FOUND, USER_ID_NOT_FOUND_MESSAGE, USER_ID_FIELD)
    assert ReadAllStoredUsers(syncEngine) == usersBefore


async def test_cu03_fetch_shows_the_user_with_the_delete_prompt(harness: UserAdminHarness) -> None:
    """ENTER on COUSR03 shows the stored fields and the PF5 delete prompt, nothing deleted."""
    user = harness.createUser(REGULAR_USER_TYPE)
    storedUser = ReadStoredUser(harness.syncEngine, user.userId)
    response = await FetchUser(harness.adminClient, user.userId, UserFetchScreen.DELETE)
    assert response.status_code == HTTP_OK, response.text
    fetchedUser = response.json()
    assert fetchedUser["message"] == PRESS_PF5_TO_DELETE_MESSAGE
    assert fetchedUser["user_id"] == user.userId
    assert fetchedUser["user_type"] == REGULAR_USER_TYPE
    AssertNoSecrets(response, user.password, storedUser["password_hash"])
    assert ReadStoredUser(harness.syncEngine, user.userId) == storedUser


async def test_cu03_delete_removes_the_user_once(harness: UserAdminHarness) -> None:
    """F5 deletes the row; a second delete and a later fetch find no such user."""
    user = harness.createUser(REGULAR_USER_TYPE)
    response = await DeleteUser(harness.adminClient, user.userId)
    AssertMessage(response, USER_DELETED_TEMPLATE.format(userId=user.userId), USER_ID_FIELD)
    assert ReadStoredUser(harness.syncEngine, user.userId) is None
    againResponse = await DeleteUser(harness.adminClient, user.userId)
    AssertLegacyError(againResponse, HTTP_NOT_FOUND, USER_ID_NOT_FOUND_MESSAGE, USER_ID_FIELD)
    fetchResponse = await FetchUser(harness.adminClient, user.userId, UserFetchScreen.DELETE)
    AssertLegacyError(fetchResponse, HTTP_NOT_FOUND, USER_ID_NOT_FOUND_MESSAGE, USER_ID_FIELD)


async def test_cu03_deleted_user_session_holds_for_its_lifetime(
    harness: UserAdminHarness, loginAs: Callable[[str], Awaitable[Any]]
) -> None:
    """A deleted user's signed-on session still reaches the main menu; a new sign-on fails."""
    signedOn = await loginAs(REGULAR_USER_TYPE)
    deleteResponse = await DeleteUser(harness.adminClient, signedOn.user.userId)
    assert deleteResponse.status_code == HTTP_OK, deleteResponse.text
    menuResponse = await signedOn.client.get(MAIN_MENU_URL)
    assert menuResponse.status_code == HTTP_OK, menuResponse.text
    signOnResponse = await SignOn(
        harness.anonymousClient, signedOn.user.userId, signedOn.user.password
    )
    AssertLegacyError(
        signOnResponse, HTTP_SIGN_ON_FAILURE, SIGN_ON_USER_NOT_FOUND_MESSAGE, USER_ID_FIELD
    )


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
async def test_regular_user_is_refused_every_endpoint(
    regularClient: httpx.AsyncClient, syncEngine: Engine, endpoint: Endpoint
) -> None:
    """User type U gets 403 with the admin-only literal, trailing space included."""
    usersBefore = ReadAllStoredUsers(syncEngine)
    response = await regularClient.request(
        endpoint.method, endpoint.url, json=EndpointBody(endpoint)
    )
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, ADMIN_ONLY_MESSAGE)
    assert ReadAllStoredUsers(syncEngine) == usersBefore


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
async def test_anonymous_client_is_refused_every_endpoint(
    apiClient: httpx.AsyncClient, syncEngine: Engine, endpoint: Endpoint
) -> None:
    """Without a session cookie every endpoint answers 401 and changes nothing."""
    usersBefore = ReadAllStoredUsers(syncEngine)
    response = await apiClient.request(endpoint.method, endpoint.url, json=EndpointBody(endpoint))
    AssertLegacyError(response, HTTP_SIGN_ON_FAILURE, NO_MESSAGE)
    assert ReadAllStoredUsers(syncEngine) == usersBefore


@pytest.mark.parametrize("origin", REFUSED_ORIGINS)
@pytest.mark.parametrize("endpoint", WRITE_ENDPOINTS)
async def test_write_without_allow_listed_origin_is_refused(
    adminClient: httpx.AsyncClient, syncEngine: Engine, endpoint: Endpoint, origin: str | None
) -> None:
    """An admin's POST, PUT or DELETE with no or a foreign Origin is 403 and changes nothing."""
    usersBefore = ReadAllStoredUsers(syncEngine)
    response = await SendWithOrigin(adminClient, endpoint, origin)
    AssertLegacyError(response, HTTP_ACCESS_REFUSED, NO_MESSAGE)
    assert ReadAllStoredUsers(syncEngine) == usersBefore
