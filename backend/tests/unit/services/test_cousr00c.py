"""Unit tests for COUSR00C (CU00), the COUSR00 user list, over the seeded test database.

cbl/COUSR00C.cbl shows USRSEC 10 users per page (USER-REC OCCURS 10 TIMES, line 57) in key
order. PROCESS-ENTER-KEY (lines 149-232) processes a selection code first: U or u transfers to
COUSR02C and D or d to COUSR03C (lines 189-209); any other code sets the invalid-selection
literal and processing continues. The browse then starts at the search key, inclusive, as
ENTER skips no READNEXT after STARTBR GTEQ (line 288); a blank key is LOW-VALUES (line 219)
and every search moves 0 to the page number (line 227). PROCESS-PF8-KEY (lines 260-277) reads
after the last key shown and PROCESS-PF7-KEY (lines 237-255) before the first key shown.
Expected values are typed from the source:

- STARTBR NOTFND: ``You are at the top of the page...`` (line 603);
- READNEXT ENDFILE: ``You have reached the bottom of the page...`` (line 637);
- READPREV ENDFILE: ``You have reached the top of the page...`` (line 671);
- F7 on page 1: ``You are already at the top of the page...`` (line 251);
- F8 with NEXT-PAGE-NO: ``You are already at the bottom of the page...`` (line 273);
- an invalid code: ``Invalid selection. Valid values are U and D`` (line 212);
- any other file condition: ``Unable to lookup User...`` (line 610).

Every path ends with MOVE -1 TO USRIDINL, so the cursor is on the search field. The statuses
are those app.services.users.cousr00c declares: a list response for every page it builds; a
400 LegacyMessageError for an F7 or F8 that reads no row, as the screen keeps its rows, and for
an invalid code the browsed page still shows; a 503 for a file condition.

MixedUsers commits users a1, AA, A1, 1A and ZZ000001-ZZ000005 beside the ten seed users. The
expected order is every user ID sorted by its code-page-037 bytes, so lower case precedes upper
case and digits sort last, as the VSAM key order did. Every case asserts that the call leaves
security.users unchanged: the browse writes nothing.
"""

import contextlib
import secrets
from collections.abc import Awaitable, Callable, Iterator
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import Engine, text

from app.core import messages
from app.core.errors import FileIoError, LegacyMessageError
from app.core.locks import FileNotOpenError
from app.core.security import HashPassword
from app.repositories.user_repository import UserRepository
from app.schemas.users import (
    UserListQuery,
    UserListResponse,
    UserSelection,
    UserSelectionRequest,
    UserSelectionResponse,
)
from app.services.users import cousr00c

# --- Users MixedUsers adds to the ten seed users -------------------------------------------------
MIXED_CASE_USER_IDS = ("a1", "AA", "A1", "1A")
EXTRA_USER_IDS = ("ZZ000001", "ZZ000002", "ZZ000003", "ZZ000004", "ZZ000005")
ADDED_USER_IDS = MIXED_CASE_USER_IDS + EXTRA_USER_IDS
ADDED_USER_NAME = "TEST"
ADDED_USER_TYPE = "U"
PASSWORD_TOKEN_BYTES = 6
SEED_USER_COUNT = 10
TOTAL_USER_COUNT = SEED_USER_COUNT + len(ADDED_USER_IDS)
EBCDIC_CODEC = "cp037"
# The first page in EBCDIC order: lower case (X'81') before upper case (X'C1'), and within the
# A keys A (X'C1') before D (X'C4') before the digit 1 (X'F1').
EXPECTED_FIRST_PAGE_IDS = [
    "a1",
    "AA",
    "ADMIN001",
    "ADMIN002",
    "ADMIN003",
    "ADMIN004",
    "ADMIN005",
    "A1",
    "USER0001",
    "USER0002",
]
# The only digit-led user ID sorts after every letter-led one.
LAST_USER_ID = "1A"

# --- Screen and paging ---------------------------------------------------------------------------
# WS-USER-DATA, USER-REC OCCURS 10 TIMES (line 57).
PAGE_ROWS = 10
ENTRY_PAGE = 0
FIRST_PAGE = 1
SECOND_PAGE = 2
FORWARD = "forward"
BACKWARD = "backward"
SEARCH_CURSOR_FIELD = "search_user_id"
EDIT_FAILURE_STATUS = 400
LOOKUP_ERROR_STATUS = 503
LOOKUP_FIELD_ERRORS = (SEARCH_CURSOR_FIELD,)
NO_MESSAGE = ""
NO_KEY = ""
NO_CODE = ""
ADMIN_USER_TYPE = "A"
EXACT_SEARCH_KEY = "ADMIN001"
# A prefix of ADMIN001 that no user ID equals: GTEQ positions on ADMIN001.
PARTIAL_SEARCH_KEY = "ADMIN00"
SEARCH_FIRST_USER_ID = "ADMIN001"
# Digits sort last in EBCDIC and 9 is the highest digit, so no user ID is at or after this key.
UNMATCHED_SEARCH_KEY = "99999999"
# The first added ZZ user: Z sorts after every other letter, so fewer than 11 user IDs are at or
# after it and its browse reaches ENDFILE before the look-ahead READNEXT.
END_OF_FILE_SEARCH_KEY = EXTRA_USER_IDS[0]
BLANK_SEARCH_KEYS = ["", "        "]
USER_FILE_NAME = "USRSEC"

# --- COUSR00C literals and routes ----------------------------------------------------------------
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
REACHED_TOP_MESSAGE = "You have reached the top of the page..."
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid values are U and D"
UNABLE_TO_LOOKUP_USER_MESSAGE = "Unable to lookup User..."
SELECTED_USER_ID = "USER0001"
UPDATE_ROUTE = "/admin/users/update?userId=USER0001"
DELETE_ROUTE = "/admin/users/delete?userId=USER0001"
INVALID_SELECTION_CODE = "X"

# --- SQL: identifiers only, values always bound --------------------------------------------------
INSERT_USER_SQL = text(
    "INSERT INTO security.users (user_id, first_name, last_name, password_hash, user_type)"
    " VALUES (:userId, :firstName, :lastName, :passwordHash, :userType)"
)
SELECT_USER_IDS_SQL = text("SELECT user_id FROM security.users")
SELECT_USERS_SQL = text("SELECT * FROM security.users ORDER BY user_id_sort")

# The core.messages paging constants, each with the COUSR00C literal it must equal.
CORE_PAGING_MESSAGES = [
    (messages.ALREADY_AT_TOP_MESSAGE, ALREADY_AT_TOP_MESSAGE),
    (messages.ALREADY_AT_BOTTOM_MESSAGE, ALREADY_AT_BOTTOM_MESSAGE),
    (messages.AT_TOP_OF_PAGE_MESSAGE, AT_TOP_OF_PAGE_MESSAGE),
    (messages.REACHED_BOTTOM_MESSAGE, REACHED_BOTTOM_MESSAGE),
    (messages.REACHED_TOP_MESSAGE, REACHED_TOP_MESSAGE),
]
CORE_PAGING_MESSAGE_IDS = [
    "already-at-top",
    "already-at-bottom",
    "at-top-of-page",
    "reached-bottom",
    "reached-top",
]

type ContextFactory = Callable[..., Awaitable[object]]


def EbcdicKey(userId: str) -> bytes:
    """Return the code-page-037 bytes of a user ID, the order VSAM kept USRSEC keys in."""
    return userId.encode(EBCDIC_CODEC)


def AddedUserRow(userId: str, bcryptRounds: int) -> dict[str, str]:
    """Return the bound values of one added user, with a generated password's hash."""
    return {
        "userId": userId,
        "firstName": ADDED_USER_NAME,
        "lastName": ADDED_USER_NAME,
        "passwordHash": HashPassword(secrets.token_urlsafe(PASSWORD_TOKEN_BYTES), bcryptRounds),
        "userType": ADDED_USER_TYPE,
    }


@pytest.fixture(name="mixedUsers")
def MixedUsers(restoreSeedState, syncEngine: Engine, testSettings) -> list[str]:
    """Commit the added users beside the seed users; return every user ID in EBCDIC order.

    restoreSeedState removes the added users again at teardown.
    """
    addedRows = [AddedUserRow(userId, testSettings.BCRYPT_ROUNDS) for userId in ADDED_USER_IDS]
    with syncEngine.begin() as connection:
        connection.execute(INSERT_USER_SQL, addedRows)
        userIds = connection.scalars(SELECT_USER_IDS_SQL).all()
    return sorted(userIds, key=EbcdicKey)


def RowIds(response: UserListResponse) -> list[str]:
    """Return the user IDs of a list response's rows, in screen order."""
    return [row.user_id for row in response.rows]


def ReadUsers(syncEngine: Engine) -> list[dict[str, object]]:
    """Return every committed security.users record, in key order."""
    with syncEngine.connect() as connection:
        return [dict(row) for row in connection.execute(SELECT_USERS_SQL).mappings()]


@contextlib.contextmanager
def NoUserWrites(syncEngine: Engine) -> Iterator[None]:
    """Assert that the block leaves every committed security.users record as it was."""
    usersBefore = ReadUsers(syncEngine)
    yield
    assert ReadUsers(syncEngine) == usersBefore


def RefusalBody(message: str, fieldErrors: tuple[str, ...] = ()) -> dict[str, object]:
    """Return the four standard keys of a refusal body, with the cursor on the search field.

    A paging or invalid-selection refusal sets no ERR-FLG and only moves the cursor, so it
    lists no field unless ``fieldErrors`` names one.
    """
    return {
        "message": message,
        "info_message": "",
        "field_errors": list(fieldErrors),
        "cursor_field": SEARCH_CURSOR_FIELD,
    }


def AssertListPage(
    response: UserListResponse, expectedIds: list[str], page: int, message: str
) -> None:
    """Assert a list response's rows, page number, first and last keys, message and cursor."""
    assert RowIds(response) == expectedIds
    assert response.page == page
    assert (response.first_key, response.last_key) == (expectedIds[0], expectedIds[-1])
    assert (response.message, response.cursor_field) == (message, SEARCH_CURSOR_FIELD)


def AssertRefusal(refusal: LegacyMessageError, message: str, status: int) -> None:
    """Assert a refusal's literal, status, cursor field and four-key body.

    The 503 WHEN OTHER lookup failure, which sets ERR-FLG, keeps the first-failure contract and
    lists the search field; a 400 refusal lists none.
    """
    assert (refusal.message, refusal.httpStatus) == (message, status)
    assert refusal.field == SEARCH_CURSOR_FIELD
    fieldErrors = LOOKUP_FIELD_ERRORS if status == LOOKUP_ERROR_STATUS else ()
    assert refusal.Body() == RefusalBody(message, fieldErrors)


async def ListPage(makeOnlineContext: ContextFactory, query: UserListQuery) -> UserListResponse:
    """Run ListUsers in a new admin context: one CU00 key press, as one request."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    return await cousr00c.ListUsers(context, query)


async def RefusedListPage(
    makeOnlineContext: ContextFactory, query: UserListQuery
) -> LegacyMessageError:
    """Run ListUsers in a new admin context and return the LegacyMessageError it raises."""
    with pytest.raises(LegacyMessageError) as raised:
        await ListPage(makeOnlineContext, query)
    return raised.value


async def SelectOnPage(
    makeOnlineContext: ContextFactory, request: UserSelectionRequest
) -> UserSelectionResponse:
    """Run ProcessUserSelection in a new admin context: one ENTER with selection codes."""
    context = await makeOnlineContext(ADMIN_USER_TYPE)
    return await cousr00c.ProcessUserSelection(context, request)


def NextPageQuery(shownPage: UserListResponse) -> UserListQuery:
    """Return the F8 query of the shown page: forward after its last key, from its page."""
    return UserListQuery(startKey=shownPage.last_key, direction=FORWARD, page=shownPage.page)


def PreviousPageQuery(shownPage: UserListResponse) -> UserListQuery:
    """Return the F7 query of the shown page: backward before its first key, from its page."""
    return UserListQuery(startKey=shownPage.first_key, direction=BACKWARD, page=shownPage.page)


def ShownSelections(shownPage: UserListResponse, code: str) -> list[UserSelection]:
    """Return the shown page's rows as keyed, with ``code`` on SELECTED_USER_ID's row only."""
    return [
        UserSelection(
            user_id=row.user_id,
            selection=code if row.user_id == SELECTED_USER_ID else NO_CODE,
        )
        for row in shownPage.rows
    ]


@pytest.mark.parametrize(
    ("coreMessage", "sourceLiteral"), CORE_PAGING_MESSAGES, ids=CORE_PAGING_MESSAGE_IDS
)
def test_core_paging_messages_are_the_cousr00c_literals(coreMessage, sourceLiteral):
    """The shared paging constants the service returns are the COUSR00C literals verbatim."""
    assert coreMessage == sourceLiteral


async def test_first_entry_shows_the_first_ten_users_in_ebcdic_order(
    mixedUsers, makeOnlineContext, syncEngine
):
    """The first entry browses from LOW-VALUES: page 1, a next page and no message."""
    with NoUserWrites(syncEngine):
        firstPage = await ListPage(makeOnlineContext, UserListQuery())
    assert len(mixedUsers) == TOTAL_USER_COUNT
    assert (mixedUsers[0], mixedUsers[-1]) == (MIXED_CASE_USER_IDS[0], LAST_USER_ID)
    assert mixedUsers[:PAGE_ROWS] == EXPECTED_FIRST_PAGE_IDS
    AssertListPage(firstPage, EXPECTED_FIRST_PAGE_IDS, FIRST_PAGE, NO_MESSAGE)
    assert firstPage.rows[0].model_dump() == {
        "user_id": MIXED_CASE_USER_IDS[0],
        "first_name": ADDED_USER_NAME,
        "last_name": ADDED_USER_NAME,
        "user_type": ADDED_USER_TYPE,
    }


async def test_next_page_shows_the_remaining_users_with_the_bottom_literal(
    mixedUsers, makeOnlineContext, syncEngine
):
    """F8 reads after the last key shown; ENDFILE before the look-ahead sets the literal."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    with NoUserWrites(syncEngine):
        secondPage = await ListPage(makeOnlineContext, NextPageQuery(firstPage))
    assert mixedUsers[PAGE_ROWS:][-1] == LAST_USER_ID
    AssertListPage(secondPage, mixedUsers[PAGE_ROWS:], SECOND_PAGE, REACHED_BOTTOM_MESSAGE)


async def test_next_page_after_the_last_page_is_refused_at_the_bottom(
    mixedUsers, makeOnlineContext, syncEngine
):
    """F8 again finds no user after the last key: NEXT-PAGE-NO, a 400 that keeps the rows."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    secondPage = await ListPage(makeOnlineContext, NextPageQuery(firstPage))
    with NoUserWrites(syncEngine):
        refusal = await RefusedListPage(makeOnlineContext, NextPageQuery(secondPage))
    assert secondPage.last_key == mixedUsers[-1]
    AssertRefusal(refusal, ALREADY_AT_BOTTOM_MESSAGE, EDIT_FAILURE_STATUS)


async def test_previous_page_from_the_second_page_returns_to_the_first(
    mixedUsers, makeOnlineContext, syncEngine
):
    """F7 reads the 10 users before the first key shown; READPREV ENDFILE resets page 1."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    secondPage = await ListPage(makeOnlineContext, NextPageQuery(firstPage))
    with NoUserWrites(syncEngine):
        backPage = await ListPage(makeOnlineContext, PreviousPageQuery(secondPage))
    AssertListPage(backPage, mixedUsers[:PAGE_ROWS], FIRST_PAGE, REACHED_TOP_MESSAGE)


async def test_previous_page_on_the_first_page_is_refused_at_the_top(
    mixedUsers, makeOnlineContext, syncEngine
):
    """F7 on page 1 reads nothing: a 400 that keeps the rows shown."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    with NoUserWrites(syncEngine):
        refusal = await RefusedListPage(makeOnlineContext, PreviousPageQuery(firstPage))
    assert firstPage.page == FIRST_PAGE
    AssertRefusal(refusal, ALREADY_AT_TOP_MESSAGE, EDIT_FAILURE_STATUS)


async def test_previous_page_from_a_key_past_every_user_is_refused_at_the_top(
    mixedUsers, makeOnlineContext, syncEngine
):
    """F7's STARTBR finds no user ID at or after the first key: NOTFND, a 400."""
    query = UserListQuery(startKey=UNMATCHED_SEARCH_KEY, direction=BACKWARD, page=SECOND_PAGE)
    with NoUserWrites(syncEngine):
        refusal = await RefusedListPage(makeOnlineContext, query)
    assert EbcdicKey(mixedUsers[-1]) < EbcdicKey(UNMATCHED_SEARCH_KEY)
    AssertRefusal(refusal, AT_TOP_OF_PAGE_MESSAGE, EDIT_FAILURE_STATUS)


async def test_search_with_no_user_at_or_after_its_key_answers_the_top_literal(
    mixedUsers, makeOnlineContext, syncEngine
):
    """ENTER's STARTBR NOTFND: page 1 with no rows shown and a blank last key."""
    query = UserListQuery(searchUserId=UNMATCHED_SEARCH_KEY, page=SECOND_PAGE)
    with NoUserWrites(syncEngine):
        notFoundPage = await ListPage(makeOnlineContext, query)
    assert (notFoundPage.rows, notFoundPage.page) == ([], FIRST_PAGE)
    assert (notFoundPage.first_key, notFoundPage.last_key) == (NO_KEY, NO_KEY)
    assert notFoundPage.message == AT_TOP_OF_PAGE_MESSAGE
    assert notFoundPage.cursor_field == SEARCH_CURSOR_FIELD


@pytest.mark.parametrize(
    "searchKey", [EXACT_SEARCH_KEY, PARTIAL_SEARCH_KEY], ids=["exact", "prefix"]
)
async def test_search_starts_at_the_first_user_id_at_or_after_its_key(
    mixedUsers, makeOnlineContext, syncEngine, searchKey
):
    """ENTER browses from the search key, inclusive, and resets the page number to 1."""
    query = UserListQuery(searchUserId=searchKey, page=SECOND_PAGE)
    with NoUserWrites(syncEngine):
        searchPage = await ListPage(makeOnlineContext, query)
    firstIndex = mixedUsers.index(SEARCH_FIRST_USER_ID)
    assert all(EbcdicKey(userId) < EbcdicKey(searchKey) for userId in mixedUsers[:firstIndex])
    expectedIds = mixedUsers[firstIndex : firstIndex + PAGE_ROWS]
    AssertListPage(searchPage, expectedIds, FIRST_PAGE, NO_MESSAGE)


@pytest.mark.parametrize("searchKey", BLANK_SEARCH_KEYS, ids=["empty", "spaces"])
async def test_blank_search_starts_at_the_lowest_key(
    mixedUsers, makeOnlineContext, syncEngine, searchKey
):
    """A blank search key is LOW-VALUES: the first page again, as page 1."""
    query = UserListQuery(searchUserId=searchKey, page=SECOND_PAGE)
    with NoUserWrites(syncEngine):
        searchPage = await ListPage(makeOnlineContext, query)
    AssertListPage(searchPage, mixedUsers[:PAGE_ROWS], FIRST_PAGE, NO_MESSAGE)


class SelectionRoute(NamedTuple):
    """A selection code keyed on SELECTED_USER_ID's row, and the route it transfers to."""

    code: str
    expectedRoute: str


SELECTION_ROUTES = [
    SelectionRoute("U", UPDATE_ROUTE),
    SelectionRoute("u", UPDATE_ROUTE),
    SelectionRoute("D", DELETE_ROUTE),
    SelectionRoute("d", DELETE_ROUTE),
]
SELECTION_ROUTE_IDS = ["U", "u", "D", "d"]
# A U or D code keyed with no search key, and with one whose browse would show another page.
SELECTION_SEARCH_KEYS = [NO_KEY, EXACT_SEARCH_KEY]
SELECTION_SEARCH_KEY_IDS = ["no-search", "with-search"]


@pytest.mark.usefixtures("mixedUsers")
@pytest.mark.parametrize("searchKey", SELECTION_SEARCH_KEYS, ids=SELECTION_SEARCH_KEY_IDS)
@pytest.mark.parametrize("selectionRoute", SELECTION_ROUTES, ids=SELECTION_ROUTE_IDS)
async def test_update_or_delete_code_routes_to_the_selected_user(
    makeOnlineContext, syncEngine, selectionRoute, searchKey
):
    """U or u transfers to the update screen and D or d to the delete screen.

    The XCTL (lines 189-209) ends the task before the search key is moved to SEC-USR-ID, so a
    keyed search key is never browsed: the selection takes precedence over the search.
    """
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    selections = ShownSelections(firstPage, selectionRoute.code)
    request = UserSelectionRequest(selections=selections, search_user_id=searchKey)
    with NoUserWrites(syncEngine), patch.object(UserRepository, "BrowseForward") as browseForward:
        selected = await SelectOnPage(makeOnlineContext, request)
    assert SELECTED_USER_ID in RowIds(firstPage)
    assert selected.route == selectionRoute.expectedRoute
    assert (selected.rows, selected.page) == ([], ENTRY_PAGE)
    assert (selected.message, selected.cursor_field) == (NO_MESSAGE, None)
    browseForward.assert_not_awaited()


async def test_invalid_code_is_refused_with_the_browsed_page(
    mixedUsers, makeOnlineContext, syncEngine
):
    """Any other code keeps its literal over a page with a next page: a 400 with the list."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    selections = ShownSelections(firstPage, INVALID_SELECTION_CODE)
    with NoUserWrites(syncEngine), pytest.raises(LegacyMessageError) as raised:
        await SelectOnPage(makeOnlineContext, UserSelectionRequest(selections=selections))
    pageKeys = {"rows": [row.model_dump() for row in firstPage.rows], "page": FIRST_PAGE}
    pageKeys |= {"first_key": mixedUsers[0], "last_key": mixedUsers[PAGE_ROWS - 1]}
    assert (raised.value.message, raised.value.httpStatus) == (
        INVALID_SELECTION_MESSAGE,
        EDIT_FAILURE_STATUS,
    )
    assert raised.value.Body() == RefusalBody(INVALID_SELECTION_MESSAGE) | pageKeys


async def test_invalid_code_with_a_search_is_refused_with_the_page_from_the_search_key(
    mixedUsers, makeOnlineContext, syncEngine
):
    """Any other code keeps its literal over the page browsed from the search key: a 400.

    The search key, not the first key shown, is moved to SEC-USR-ID after the selection (lines
    218-228): page 1 starts at ADMIN001, inclusive, and its look-ahead row keeps the literal.
    """
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    searchPage = await ListPage(makeOnlineContext, UserListQuery(searchUserId=EXACT_SEARCH_KEY))
    selections = ShownSelections(firstPage, INVALID_SELECTION_CODE)
    request = UserSelectionRequest(selections=selections, search_user_id=EXACT_SEARCH_KEY)
    with NoUserWrites(syncEngine), pytest.raises(LegacyMessageError) as raised:
        await SelectOnPage(makeOnlineContext, request)
    searchIndex = mixedUsers.index(EXACT_SEARCH_KEY)
    searchedIds = mixedUsers[searchIndex : searchIndex + PAGE_ROWS]
    assert len(mixedUsers) > searchIndex + PAGE_ROWS
    assert RowIds(searchPage) == searchedIds
    assert searchedIds != RowIds(firstPage)
    pageKeys = {"rows": [row.model_dump() for row in searchPage.rows], "page": FIRST_PAGE}
    pageKeys |= {"first_key": searchedIds[0], "last_key": searchedIds[-1]}
    assert (raised.value.message, raised.value.httpStatus) == (
        INVALID_SELECTION_MESSAGE,
        EDIT_FAILURE_STATUS,
    )
    assert raised.value.Body() == RefusalBody(INVALID_SELECTION_MESSAGE) | pageKeys


async def test_invalid_code_with_a_search_reaching_the_end_shows_the_bottom_literal(
    mixedUsers, makeOnlineContext, syncEngine
):
    """READNEXT ENDFILE in the browse from the search key replaces the invalid-code literal.

    ``You have reached the bottom of the page...`` overwrites WS-MESSAGE (lines 634-640), so the
    answer is the page browsed from the search key, as page 1, with no route.
    """
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    selections = ShownSelections(firstPage, INVALID_SELECTION_CODE)
    request = UserSelectionRequest(selections=selections, search_user_id=END_OF_FILE_SEARCH_KEY)
    with NoUserWrites(syncEngine):
        selected = await SelectOnPage(makeOnlineContext, request)
    searchedIds = mixedUsers[mixedUsers.index(END_OF_FILE_SEARCH_KEY) :]
    assert len(searchedIds) <= PAGE_ROWS
    assert not set(searchedIds) & set(RowIds(firstPage))
    assert selected.route is None
    AssertListPage(selected, searchedIds, FIRST_PAGE, REACHED_BOTTOM_MESSAGE)


async def test_invalid_code_with_an_unmatched_search_rereads_the_shown_rows(
    mixedUsers, makeOnlineContext, syncEngine
):
    """STARTBR NOTFND on the search key replaces the invalid-code literal with the top literal.

    No row is read (lines 600-606), so the rows still shown are read again from the first key
    shown, on page 1 with a blank last key and no route.
    """
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    selections = ShownSelections(firstPage, INVALID_SELECTION_CODE)
    request = UserSelectionRequest(selections=selections, search_user_id=UNMATCHED_SEARCH_KEY)
    with NoUserWrites(syncEngine):
        selected = await SelectOnPage(makeOnlineContext, request)
    assert EbcdicKey(mixedUsers[-1]) < EbcdicKey(UNMATCHED_SEARCH_KEY)
    assert selected.route is None
    assert (RowIds(selected), selected.page) == (mixedUsers[:PAGE_ROWS], FIRST_PAGE)
    assert (selected.first_key, selected.last_key) == (mixedUsers[0], NO_KEY)
    assert selected.message == AT_TOP_OF_PAGE_MESSAGE
    assert selected.cursor_field == SEARCH_CURSOR_FIELD


class ForwardBrowsePress(NamedTuple):
    """A CU00 key press that browses forward, and the BrowseForward call it makes."""

    press: Callable[[ContextFactory], Awaitable[object]]
    startKey: str | None
    inclusive: bool


async def PressFirstEntry(makeOnlineContext: ContextFactory) -> UserListResponse:
    """Enter COUSR00 from the admin menu: ENTER with no search key."""
    return await ListPage(makeOnlineContext, UserListQuery())


async def PressNextPage(makeOnlineContext: ContextFactory) -> UserListResponse:
    """Press F8 on page 1, after the first page's last key."""
    query = UserListQuery(startKey=EXPECTED_FIRST_PAGE_IDS[-1], direction=FORWARD, page=FIRST_PAGE)
    return await ListPage(makeOnlineContext, query)


async def PressInvalidSelection(makeOnlineContext: ContextFactory) -> UserSelectionResponse:
    """Press ENTER with an invalid code on SELECTED_USER_ID's row and no search key."""
    selection = UserSelection(user_id=SELECTED_USER_ID, selection=INVALID_SELECTION_CODE)
    request = UserSelectionRequest(selections=[selection])
    return await SelectOnPage(makeOnlineContext, request)


FORWARD_BROWSE_PRESSES = [
    ForwardBrowsePress(PressFirstEntry, None, True),
    ForwardBrowsePress(PressNextPage, EXPECTED_FIRST_PAGE_IDS[-1], False),
    ForwardBrowsePress(PressInvalidSelection, None, True),
]
FORWARD_BROWSE_PRESS_IDS = ["first-entry", "next-page", "invalid-selection"]


@pytest.mark.parametrize("keyPress", FORWARD_BROWSE_PRESSES, ids=FORWARD_BROWSE_PRESS_IDS)
async def test_file_condition_on_the_browse_is_unable_to_lookup_user(
    makeOnlineContext, syncEngine, monkeypatch, keyPress
):
    """NOTOPEN on the STARTBR/READNEXT browse is the WHEN OTHER literal, a 503."""
    browseForward = AsyncMock(side_effect=FileNotOpenError(USER_FILE_NAME))
    monkeypatch.setattr(UserRepository, "BrowseForward", browseForward)
    with NoUserWrites(syncEngine), pytest.raises(LegacyMessageError) as raised:
        await keyPress.press(makeOnlineContext)
    AssertRefusal(raised.value, UNABLE_TO_LOOKUP_USER_MESSAGE, LOOKUP_ERROR_STATUS)
    browseForward.assert_awaited_once_with(
        keyPress.startKey, inclusive=keyPress.inclusive, limit=PAGE_ROWS
    )


@pytest.mark.usefixtures("mixedUsers")
@pytest.mark.parametrize("conditionType", [FileIoError, FileNotOpenError], ids=["ioerr", "notopen"])
async def test_file_condition_on_the_backward_browse_is_unable_to_lookup_user(
    makeOnlineContext, syncEngine, monkeypatch, conditionType
):
    """IOERR, or the batch window's NOTOPEN, on F7's STARTBR/READPREV browse is a 503."""
    firstPage = await ListPage(makeOnlineContext, UserListQuery())
    secondPage = await ListPage(makeOnlineContext, NextPageQuery(firstPage))
    browseBackward = AsyncMock(side_effect=conditionType(USER_FILE_NAME))
    monkeypatch.setattr(UserRepository, "BrowseBackward", browseBackward)
    with NoUserWrites(syncEngine):
        refusal = await RefusedListPage(makeOnlineContext, PreviousPageQuery(secondPage))
    assert secondPage.page == SECOND_PAGE
    AssertRefusal(refusal, UNABLE_TO_LOOKUP_USER_MESSAGE, LOOKUP_ERROR_STATUS)
    browseBackward.assert_awaited_once_with(secondPage.first_key, limit=PAGE_ROWS)
