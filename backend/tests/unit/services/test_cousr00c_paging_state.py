"""Unit tests for the COUSR00C (CU00) paging state after a search that finds no user ID.

cbl/COUSR00C.cbl PROCESS-ENTER-KEY moves 0 to CDEMO-CU00-PAGE-NUM (line 227) before
PROCESS-PAGE-FORWARD. When no user ID is at or after the search key, STARTBR NOTFND (lines
600-606) sets USER-SEC-EOF but not ERR-FLG, so the browse continues with no row read: it sets
NEXT-PAGE-NO and the COUSR00 screen shows page 0 with ``You are at the top of the page...``
(lines 317-328). From page 0, PROCESS-PF8-KEY answers ``You are already at the bottom of the
page...`` (lines 270-276) and PROCESS-PF7-KEY ``You are already at the top of the page...``,
as the page is not above 1 (lines 248-255). Expected values are typed from those lines:
- ENTER, a query with no direction, refuses with 400 and a body carrying ``page`` 0 and no
    first or last key, as POPULATE-USER-DATA never ran: the first entry on an empty USRSEC,
    a search, and a search with a selection code, each browsing inclusively;
- F8 and F7 from page 0 refuse before any USRSEC read, with the shown keys or with the empty
    keys a client holds when no page was ever shown;
- F8 with an empty last key refuses at the bottom before any read on page 1 too, as a
    forward direction is never ENTER;
- F8 from page 1 still reads the next 10 users as page 2.

app.services.users.cousr00c's UserRepository is replaced by a stub whose browses are
AsyncMocks, so no database is needed.
"""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest

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
# Digits sort after letters in EBCDIC, so no letter-led user ID is at or after this key; the
# stub's BrowseForward answers it with STARTBR NOTFND.
UNMATCHED_SEARCH_KEY = "99999999"
# The first and last user IDs of the page shown when the key is pressed.
SHOWN_FIRST_KEY = "LISTU001"
SHOWN_LAST_KEY = "LISTU010"
# A row code other than U, u, D or d: PROCESS-ENTER-KEY sets the invalid-selection literal and
# continues with the search (lines 210-214).
INVALID_SELECTION_CODE = "X"
SELECTED_USER_ID = "LISTU003"
# The first and last keys a client holds when no page was ever shown; a blank search key.
NO_KEY = ""
# LOW-VALUES: the browse key of a blank search (line 219), which the stub receives as None.
LOW_VALUES_KEY = None

# The STARTBR NOTFND, PROCESS-PF8-KEY and PROCESS-PF7-KEY literals (lines 603, 273, 251).
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
# Every path ends with MOVE -1 TO USRIDINL: the cursor is on the search field.
SEARCH_CURSOR_FIELD = "search_user_id"
REFUSAL_STATUS = 400
ENTER_PAGE = 0
FIRST_PAGE = 1
NEXT_PAGE_FROM_FIRST = 2
NO_MESSAGE = ""
# The user number after SHOWN_LAST_KEY, where the next page's browse rows begin.
NUMBER_AFTER_SHOWN_LAST = 11

EXPECTED_NOT_FOUND_BODY = {
    "message": AT_TOP_OF_PAGE_MESSAGE,
    "info_message": "",
    "field_errors": [SEARCH_CURSOR_FIELD],
    "cursor_field": SEARCH_CURSOR_FIELD,
    "page": ENTER_PAGE,
}


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
    RefusedPagingKey("forward", SHOWN_LAST_KEY, ENTER_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
    RefusedPagingKey("backward", SHOWN_FIRST_KEY, ENTER_PAGE, ALREADY_AT_TOP_MESSAGE),
    RefusedPagingKey("forward", NO_KEY, ENTER_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
    RefusedPagingKey("backward", NO_KEY, ENTER_PAGE, ALREADY_AT_TOP_MESSAGE),
    RefusedPagingKey("forward", NO_KEY, FIRST_PAGE, ALREADY_AT_BOTTOM_MESSAGE),
]
REFUSED_PAGING_KEY_IDS = [
    "F8-page0",
    "F7-page0",
    "F8-page0-empty-key",
    "F7-page0-empty-key",
    "F8-page1-empty-key",
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
    """Return the four-key 400 body of a paging refusal, with the cursor on the search field."""
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


async def FirstEntry(context: OnlineContext) -> UserListResponse:
    """Enter COUSR00 from the admin menu: no direction, no search key and no paging keys."""
    return await cousr00c.ListUsers(context, UserListQuery())


async def EnterSearch(context: OnlineContext) -> UserListResponse:
    """Press ENTER on COUSR00 with the unmatched search key and no selection code."""
    query = UserListQuery(searchUserId=UNMATCHED_SEARCH_KEY)
    return await cousr00c.ListUsers(context, query)


async def EnterSelectionAndSearch(context: OnlineContext) -> UserListResponse:
    """Press ENTER with an invalid code on one row and the unmatched search key."""
    selection = UserSelection(user_id=SELECTED_USER_ID, selection=INVALID_SELECTION_CODE)
    request = UserSelectionRequest(search_user_id=UNMATCHED_SEARCH_KEY, selections=[selection])
    return await cousr00c.ProcessUserSelection(context, request)


class EnterPress(NamedTuple):
    """An ENTER on COUSR00 and the key its inclusive STARTBR browses from."""

    pressEnter: Callable[[OnlineContext], Awaitable[UserListResponse]]
    browseKey: str | None


ENTER_PRESSES = [
    EnterPress(FirstEntry, LOW_VALUES_KEY),
    EnterPress(EnterSearch, UNMATCHED_SEARCH_KEY),
    EnterPress(EnterSelectionAndSearch, UNMATCHED_SEARCH_KEY),
]
ENTER_PRESS_IDS = ["first-entry", "list", "selection"]


@pytest.mark.parametrize("enterPress", ENTER_PRESSES, ids=ENTER_PRESS_IDS)
async def test_enter_with_no_user_at_or_after_search_key_answers_page_zero(
    userRepository, enterPress
):
    """STARTBR NOTFND on ENTER: ``You are at the top of the page...``, page 0, keys unsent."""
    with pytest.raises(LegacyMessageError) as raised:
        await enterPress.pressEnter(BuildContext())
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert raised.value.Body() == EXPECTED_NOT_FOUND_BODY
    userRepository.BrowseForward.assert_awaited_once_with(
        enterPress.browseKey, inclusive=True, limit=PAGE_ROWS
    )
    userRepository.BrowseBackward.assert_not_awaited()


@pytest.mark.parametrize("refusedKey", REFUSED_PAGING_KEYS, ids=REFUSED_PAGING_KEY_IDS)
async def test_paging_key_refused_before_any_read(userRepository, refusedKey):
    """F8 from page 0 or with no last key is NEXT-PAGE-NO, F7 from page 0 a page not above 1."""
    query = UserListQuery(
        startKey=refusedKey.startKey, direction=refusedKey.direction, page=refusedKey.page
    )
    with pytest.raises(LegacyMessageError) as raised:
        await cousr00c.ListUsers(BuildContext(), query)
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert raised.value.Body() == RefusalBody(refusedKey.expectedMessage)
    userRepository.BrowseForward.assert_not_awaited()
    userRepository.BrowseBackward.assert_not_awaited()


async def test_next_page_from_page_one_reads_the_next_ten_users(userRepository):
    """F8 from page 1 with 11 users after the last key: page 2 holds the first 10."""
    usersAfterLastKey = BrowseRows(NUMBER_AFTER_SHOWN_LAST, PAGE_ROWS + 1)
    userRepository.BrowseForward.return_value = usersAfterLastKey
    query = UserListQuery(startKey=SHOWN_LAST_KEY, direction="forward", page=FIRST_PAGE)
    nextPage = await cousr00c.ListUsers(BuildContext(), query)
    expectedIds = [row["user_id"] for row in usersAfterLastKey[:PAGE_ROWS]]
    assert nextPage.page == NEXT_PAGE_FROM_FIRST
    assert [row.user_id for row in nextPage.rows] == expectedIds
    assert (nextPage.first_key, nextPage.last_key) == (expectedIds[0], expectedIds[-1])
    assert nextPage.message == NO_MESSAGE
    userRepository.BrowseForward.assert_awaited_once_with(
        SHOWN_LAST_KEY, inclusive=False, limit=PAGE_ROWS
    )
