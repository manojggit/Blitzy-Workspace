"""Unit tests for the COUSR00C (CU00) ENTER outcomes and paging state of the user list.

cbl/COUSR00C.cbl PROCESS-ENTER-KEY processes a selection code first: any code but U, u, D or d
sets ``Invalid selection. Valid values are U and D`` and processing continues (lines 210-214).
The search key then starts the browse (lines 216-228). A STARTBR NOTFND (lines 600-606) sets
``You are at the top of the page...`` and NEXT-PAGE-NO over the rows left shown (lines
317-322); an ENDFILE before the look-ahead READNEXT sets ``You have reached the bottom of the
page...`` (lines 634-640). Either literal replaces the invalid-selection literal.
PROCESS-PF8-KEY refuses NEXT-PAGE-NO with ``You are already at the bottom of the page...``
(lines 270-276) and PROCESS-PF7-KEY a page not above 1 with ``You are already at the top of
the page...`` (lines 248-255). Every ENTER search resets the page number to 1, and an edit
failure is a 400. Expected values are typed from those lines:
- ENTER finding no user ID answers 200 on page 1 with a blank last key: with no rows when no
    page is shown (the first entry, a search, a selection request with no or blank rows), and
    with the rows read again from the first row's user ID when one is;
- F8 with a blank last key and F7 on page 0 or 1 refuse with the four standard keys before any
    USRSEC read;
- F8 with a last key reads the next 10 users from any page, page 0 included, and the page
    number keeps CDEMO-CU00-PAGE-NUM's PIC 9(08) width (line 70): page 99999999 is followed by
    page 0, and a wider page is refused as malformed;
- an invalid code the browse leaves on the ERRMSG line is a 400 whose body adds the page's
    rows, page, first_key and last_key, while a page reaching the end of the file is a 200;
- U, u, D and d still route without any read.

app.services.users.cousr00c's UserRepository is replaced by a stub whose browses are
AsyncMocks, so no database is needed.
"""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, call

import pytest
from pydantic import ValidationError

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.schemas.users import (
    UserListQuery,
    UserListResponse,
    UserSelection,
    UserSelectionRequest,
)
from app.services.users import cousr00c

# WS-USER-DATA, USER-REC OCCURS 10 TIMES (line 57).
PAGE_ROWS = 10
# The re-read of a shown page asks for 9 rows and the look-ahead: 10 records in all.
SHOWN_PAGE_READ_LIMIT = PAGE_ROWS - 1
# Digits sort after letters in EBCDIC, so no letter-led user ID is at or after this key; the
# stub's BrowseForward answers it with STARTBR NOTFND.
UNMATCHED_SEARCH_KEY = "99999999"
# The first and last user IDs of the page shown when the key is pressed.
SHOWN_FIRST_NUMBER = 1
SHOWN_FIRST_KEY = "LISTU001"
SHOWN_LAST_KEY = "LISTU010"
# A row code other than U, u, D or d: PROCESS-ENTER-KEY sets the invalid-selection literal and
# continues with the search (lines 210-214). S is CT00's code, invalid on COUSR00.
INVALID_SELECTION_CODE = "X"
TRANSACTION_SELECT_CODE = "S"
NO_CODE = ""
SELECTED_USER_ID = "LISTU003"
# The first and last keys a client holds when no page was ever shown; a blank search key.
NO_KEY = ""
SPACES_KEY = "        "
# LOW-VALUES: the browse key of a blank search (line 219), which the stub receives as None.
LOW_VALUES_KEY = None
# The users at or after the search key when the page ends the file: ENDFILE before the
# look-ahead READNEXT.
END_OF_FILE_ROWS = 5
END_OF_FILE_LAST_KEY = "LISTU005"

# The STARTBR NOTFND, READNEXT ENDFILE, PROCESS-PF8-KEY, PROCESS-PF7-KEY and selection
# literals (lines 603, 637, 273, 251, 212).
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid values are U and D"
# Every path ends with MOVE -1 TO USRIDINL: the cursor is on the search field.
SEARCH_CURSOR_FIELD = "search_user_id"
REFUSAL_STATUS = 400
ENTER_PAGE = 0
FIRST_PAGE = 1
NEXT_PAGE_FROM_FIRST = 2
# CDEMO-CU00-PAGE-NUM PIC 9(08) (line 70): COMPUTE ... + 1 with no ON SIZE ERROR (lines
# 309-310) truncates 100000000 to 00000000, and no COMMAREA carries a wider page.
LARGEST_PAGE = 99999999
PAGE_AFTER_LARGEST = 0
WIDER_THAN_EIGHT_DIGIT_PAGES = ["100000000", "99999999999999999999"]
NO_MESSAGE = ""
# The user number after SHOWN_LAST_KEY, where the next page's browse rows begin.
NUMBER_AFTER_SHOWN_LAST = 11
# The update and delete routes of the selected row (XCTL COUSR02C, XCTL COUSR03C).
UPDATE_ROUTE = "/admin/users/update?userId=LISTU003"
DELETE_ROUTE = "/admin/users/delete?userId=LISTU003"


class StubUserRepository:
    """UserRepository's USRSEC browses as AsyncMocks; both answer STARTBR NOTFND by default."""

    def __init__(self) -> None:
        self.BrowseForward = AsyncMock(return_value=None)
        self.BrowseBackward = AsyncMock(return_value=None)

    def ForContext(self, context: OnlineContext) -> "StubUserRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


class RefusedPagingKey(NamedTuple):
    """A paging key, the key and page it is pressed with, and the refusal literal it answers."""

    direction: str
    startKey: str
    page: int
    expectedMessage: str


REFUSED_PAGING_KEYS = [
    RefusedPagingKey("forward", NO_KEY, FIRST_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
    RefusedPagingKey("forward", SPACES_KEY, FIRST_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
    RefusedPagingKey("forward", NO_KEY, ENTER_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
    RefusedPagingKey("backward", SHOWN_FIRST_KEY, ENTER_PAGE, ALREADY_AT_TOP_MESSAGE),
    RefusedPagingKey("backward", SHOWN_FIRST_KEY, FIRST_PAGE, ALREADY_AT_TOP_MESSAGE),
    RefusedPagingKey("backward", NO_KEY, ENTER_PAGE, ALREADY_AT_TOP_MESSAGE),
]
REFUSED_PAGING_KEY_IDS = [
    "F8-page1-empty-key",
    "F8-page1-spaces-key",
    "F8-page0-empty-key",
    "F7-page0",
    "F7-page1",
    "F7-page0-empty-key",
]


@pytest.fixture(name="userRepository")
def UserRepositoryStub(monkeypatch) -> StubUserRepository:
    """Make cousr00c build the stub wherever it builds a UserRepository, and return the stub."""
    stubRepository = StubUserRepository()
    monkeypatch.setattr(cousr00c, "UserRepository", stubRepository.ForContext)
    return stubRepository


def BuildContext() -> OnlineContext:
    """Return an online context with no request or settings, which the stub never reads."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=None)


def RefusalBody(message: str) -> dict[str, object]:
    """Return the four standard keys of a 400 body, with the cursor on the search field."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [SEARCH_CURSOR_FIELD],
        "cursor_field": SEARCH_CURSOR_FIELD,
    }


def BrowseRows(firstNumber: int, count: int) -> list[dict[str, str]]:
    """Return ``count`` USRSEC browse rows in key order, from user ID LISTU<firstNumber>."""
    return [
        {
            "user_id": f"LISTU{number:03d}",
            "first_name": f"First{number}",
            "last_name": f"Last{number}",
            "user_type": "U",
        }
        for number in range(firstNumber, firstNumber + count)
    ]


def ShownSelections(code: str) -> list[UserSelection]:
    """Return the 10 rows of the shown page in screen order, the selected row carrying ``code``."""
    return [
        UserSelection(
            user_id=row["user_id"],
            selection=code if row["user_id"] == SELECTED_USER_ID else NO_CODE,
        )
        for row in BrowseRows(SHOWN_FIRST_NUMBER, PAGE_ROWS)
    ]


async def FirstEntry(context: OnlineContext) -> UserListResponse:
    """Enter COUSR00 from the admin menu: no direction, no search key and no paging keys."""
    return await cousr00c.ListUsers(context, UserListQuery())


async def EnterSearch(context: OnlineContext) -> UserListResponse:
    """Press ENTER on COUSR00 with the unmatched search key and no selection code."""
    query = UserListQuery(searchUserId=UNMATCHED_SEARCH_KEY)
    return await cousr00c.ListUsers(context, query)


async def EnterSelectionWithNoRows(context: OnlineContext) -> UserListResponse:
    """Press ENTER through the selection route with no shown row and the unmatched key."""
    request = UserSelectionRequest(search_user_id=UNMATCHED_SEARCH_KEY)
    return await cousr00c.ProcessUserSelection(context, request)


async def EnterSelectionWithBlankFirstRow(context: OnlineContext) -> UserListResponse:
    """Press ENTER with an invalid code on a row whose user ID is blank, as no page is shown."""
    selection = UserSelection(user_id=SPACES_KEY, selection=INVALID_SELECTION_CODE)
    request = UserSelectionRequest(search_user_id=UNMATCHED_SEARCH_KEY, selections=[selection])
    return await cousr00c.ProcessUserSelection(context, request)


class EnterPress(NamedTuple):
    """An ENTER on COUSR00 with no page shown, and the key its inclusive STARTBR browses from."""

    pressEnter: Callable[[OnlineContext], Awaitable[UserListResponse]]
    browseKey: str | None


ENTER_PRESSES = [
    EnterPress(FirstEntry, LOW_VALUES_KEY),
    EnterPress(EnterSearch, UNMATCHED_SEARCH_KEY),
    EnterPress(EnterSelectionWithNoRows, UNMATCHED_SEARCH_KEY),
    EnterPress(EnterSelectionWithBlankFirstRow, UNMATCHED_SEARCH_KEY),
]
ENTER_PRESS_IDS = ["first-entry", "list-search", "selection-no-rows", "selection-blank-row"]


@pytest.mark.parametrize("enterPress", ENTER_PRESSES, ids=ENTER_PRESS_IDS)
async def test_search_finding_nothing_with_no_page_shown_answers_empty_page_one(
    userRepository, enterPress
):
    """STARTBR NOTFND: 200 on page 1, no rows, blank keys, and no re-read of a shown page."""
    notFoundPage = await enterPress.pressEnter(BuildContext())
    assert (notFoundPage.page, notFoundPage.rows) == (FIRST_PAGE, [])
    assert (notFoundPage.first_key, notFoundPage.last_key) == (NO_KEY, NO_KEY)
    assert notFoundPage.message == AT_TOP_OF_PAGE_MESSAGE
    assert notFoundPage.cursor_field == SEARCH_CURSOR_FIELD
    userRepository.BrowseForward.assert_awaited_once_with(
        enterPress.browseKey, inclusive=True, limit=PAGE_ROWS
    )
    userRepository.BrowseBackward.assert_not_awaited()


class ShownPageReread(NamedTuple):
    """A selection code on the shown page, and what the re-read from its first key finds."""

    code: str
    rereadRecords: list[dict[str, str]] | None
    expectedFirstKey: str


SHOWN_PAGE_REREADS = [
    ShownPageReread(
        INVALID_SELECTION_CODE, BrowseRows(SHOWN_FIRST_NUMBER, PAGE_ROWS), SHOWN_FIRST_KEY
    ),
    ShownPageReread(NO_CODE, BrowseRows(SHOWN_FIRST_NUMBER, PAGE_ROWS), SHOWN_FIRST_KEY),
    ShownPageReread(INVALID_SELECTION_CODE, None, NO_KEY),
]
SHOWN_PAGE_REREAD_IDS = ["invalid-code", "no-code", "shown-users-gone"]


@pytest.mark.parametrize("reread", SHOWN_PAGE_REREADS, ids=SHOWN_PAGE_REREAD_IDS)
async def test_search_finding_nothing_rereads_the_shown_page_on_page_one(userRepository, reread):
    """STARTBR NOTFND over a shown page: 200 on page 1, its rows re-read, a blank last key."""
    userRepository.BrowseForward.side_effect = [None, reread.rereadRecords]
    request = UserSelectionRequest(
        search_user_id=UNMATCHED_SEARCH_KEY, selections=ShownSelections(reread.code)
    )
    notFoundPage = await cousr00c.ProcessUserSelection(BuildContext(), request)
    assert [row.model_dump() for row in notFoundPage.rows] == (reread.rereadRecords or [])
    assert (notFoundPage.page, notFoundPage.route) == (FIRST_PAGE, None)
    assert (notFoundPage.first_key, notFoundPage.last_key) == (reread.expectedFirstKey, NO_KEY)
    assert notFoundPage.message == AT_TOP_OF_PAGE_MESSAGE
    assert userRepository.BrowseForward.await_args_list == [
        call(UNMATCHED_SEARCH_KEY, inclusive=True, limit=PAGE_ROWS),
        call(SHOWN_FIRST_KEY, inclusive=True, limit=SHOWN_PAGE_READ_LIMIT),
    ]


@pytest.mark.parametrize("refusedKey", REFUSED_PAGING_KEYS, ids=REFUSED_PAGING_KEY_IDS)
async def test_paging_key_refused_before_any_read(userRepository, refusedKey):
    """F8 with a blank last key is NEXT-PAGE-NO, F7 on page 0 or 1 a page not above 1."""
    query = UserListQuery(
        startKey=refusedKey.startKey, direction=refusedKey.direction, page=refusedKey.page
    )
    with pytest.raises(LegacyMessageError) as raised:
        await cousr00c.ListUsers(BuildContext(), query)
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert raised.value.Body() == RefusalBody(refusedKey.expectedMessage)
    userRepository.BrowseForward.assert_not_awaited()
    userRepository.BrowseBackward.assert_not_awaited()


@pytest.mark.parametrize(
    ("shownPage", "expectedPage"),
    [
        (ENTER_PAGE, FIRST_PAGE),
        (FIRST_PAGE, NEXT_PAGE_FROM_FIRST),
        (LARGEST_PAGE, PAGE_AFTER_LARGEST),
    ],
    ids=["page0", "page1", "page99999999-wraps"],
)
async def test_next_page_reads_the_next_ten_users_from_any_page(
    userRepository, shownPage, expectedPage
):
    """F8 with a last key and 11 users after it: the next page holds the first 10."""
    usersAfterLastKey = BrowseRows(NUMBER_AFTER_SHOWN_LAST, PAGE_ROWS + 1)
    userRepository.BrowseForward.return_value = usersAfterLastKey
    query = UserListQuery(startKey=SHOWN_LAST_KEY, direction="forward", page=shownPage)
    nextPage = await cousr00c.ListUsers(BuildContext(), query)
    expectedIds = [row["user_id"] for row in usersAfterLastKey[:PAGE_ROWS]]
    assert nextPage.page == expectedPage
    assert [row.user_id for row in nextPage.rows] == expectedIds
    assert (nextPage.first_key, nextPage.last_key) == (expectedIds[0], expectedIds[-1])
    assert nextPage.message == NO_MESSAGE
    userRepository.BrowseForward.assert_awaited_once_with(
        SHOWN_LAST_KEY, inclusive=False, limit=PAGE_ROWS
    )


@pytest.mark.parametrize("pageText", WIDER_THAN_EIGHT_DIGIT_PAGES, ids=["9-digits", "20-digits"])
def test_page_wider_than_eight_digits_is_malformed(pageText):
    """The query model refuses the page, which the route answers with 422 before the service."""
    wireQuery = {"startKey": SHOWN_LAST_KEY, "direction": "forward", "page": pageText}
    with pytest.raises(ValidationError) as raised:
        UserListQuery.model_validate(wireQuery)
    assert [error["loc"] for error in raised.value.errors()] == [("page",)]


@pytest.mark.parametrize(
    ("code", "searchKey", "browseKey"),
    [
        (INVALID_SELECTION_CODE, NO_KEY, LOW_VALUES_KEY),
        (TRANSACTION_SELECT_CODE, SHOWN_FIRST_KEY, SHOWN_FIRST_KEY),
    ],
    ids=["X-blank-search", "S-keyed-search"],
)
async def test_invalid_selection_on_a_page_with_a_next_page_is_an_edit_failure(
    userRepository, code, searchKey, browseKey
):
    """The literal stays: 400 with the four standard keys plus the browsed page's list keys."""
    browsedUsers = BrowseRows(SHOWN_FIRST_NUMBER, PAGE_ROWS + 1)
    userRepository.BrowseForward.return_value = browsedUsers
    request = UserSelectionRequest(search_user_id=searchKey, selections=ShownSelections(code))
    with pytest.raises(LegacyMessageError) as raised:
        await cousr00c.ProcessUserSelection(BuildContext(), request)
    pageKeys = {"rows": browsedUsers[:PAGE_ROWS], "page": FIRST_PAGE}
    pageKeys |= {"first_key": SHOWN_FIRST_KEY, "last_key": SHOWN_LAST_KEY}
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert raised.value.Body() == RefusalBody(INVALID_SELECTION_MESSAGE) | pageKeys
    userRepository.BrowseForward.assert_awaited_once_with(
        browseKey, inclusive=True, limit=PAGE_ROWS
    )


async def test_invalid_selection_on_a_page_ending_the_file_answers_reached_bottom(
    userRepository,
):
    """ENDFILE before the look-ahead replaces the literal: 200 with the bottom literal."""
    browsedUsers = BrowseRows(SHOWN_FIRST_NUMBER, END_OF_FILE_ROWS)
    userRepository.BrowseForward.return_value = browsedUsers
    request = UserSelectionRequest(selections=ShownSelections(INVALID_SELECTION_CODE))
    lastPage = await cousr00c.ProcessUserSelection(BuildContext(), request)
    assert [row.model_dump() for row in lastPage.rows] == browsedUsers
    assert (lastPage.page, lastPage.route) == (FIRST_PAGE, None)
    assert (lastPage.first_key, lastPage.last_key) == (SHOWN_FIRST_KEY, END_OF_FILE_LAST_KEY)
    assert lastPage.message == REACHED_BOTTOM_MESSAGE
    userRepository.BrowseForward.assert_awaited_once_with(
        LOW_VALUES_KEY, inclusive=True, limit=PAGE_ROWS
    )


@pytest.mark.parametrize(
    ("code", "expectedRoute"),
    [("U", UPDATE_ROUTE), ("u", UPDATE_ROUTE), ("D", DELETE_ROUTE), ("d", DELETE_ROUTE)],
)
async def test_update_or_delete_code_routes_without_any_read(userRepository, code, expectedRoute):
    """U, u, D and d end the request as XCTL did: the route, and no USRSEC browse."""
    request = UserSelectionRequest(
        search_user_id=UNMATCHED_SEARCH_KEY, selections=ShownSelections(code)
    )
    selected = await cousr00c.ProcessUserSelection(BuildContext(), request)
    assert selected.route == expectedRoute
    userRepository.BrowseForward.assert_not_awaited()
    userRepository.BrowseBackward.assert_not_awaited()
