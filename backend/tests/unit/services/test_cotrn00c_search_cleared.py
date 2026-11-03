"""Unit tests for the COTRN00C (CT00) TRNIDIN blanking that ``search_cleared`` reports.

cbl/COTRN00C.cbl blanks the search field with MOVE SPACE TO TRNIDINO in two places: line 228,
after PROCESS-ENTER-KEY when ERR-FLG is off, and line 325, at the end of the PROCESS-PAGE-FORWARD
body (lines 283-328), which runs whenever STARTBR sets no ERR-FLG. STARTBR NOTFND sets only
TRANSACT-EOF (lines 605-611), so that body still runs. Expected values are typed from those
lines:
- blanked: the first entry and ENTER with a blank or numeric key (lines 206-210), whether the
    browse finds rows or STARTBR is NOTFND, an invalid selection code with a numeric key, which
    sets its literal and continues (lines 196-201), and an F8 that pages on (lines 267-268);
- kept: a non-numeric key, which sets ERR-FLG (lines 211-217), a valid S selection, which
    transfers to COTRN01C (lines 186-195), an F8 refusal (lines 269-274), and every F7, as
    PROCESS-PF7-KEY and PROCESS-PAGE-BACKWARD (lines 234-252, 333-376) never move to TRNIDINO.

app.services.transactions.cotrn00c's TransactionRepository is replaced by a stub whose browses
are AsyncMocks, so no database is needed.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.schemas.transactions import (
    TransactionListQuery,
    TransactionListResponse,
    TransactionSelection,
    TransactionSelectionRequest,
)
from app.services.transactions import cotrn00c

# The first and last TRAN-IDs of the page shown when the key is pressed, and its page number.
SHOWN_FIRST_KEY = "0000000000000001"
SHOWN_LAST_KEY = "0000000000000010"
SHOWN_PAGE = 1
# The first TRAN-ID of a second page, from which F7 reads backward.
SECOND_PAGE_FIRST_KEY = "0000000000000011"
SECOND_PAGE = 2
# All 16 positions are digits, so TRNIDINI IS NUMERIC holds (line 209); the other key fails it.
NUMERIC_SEARCH_KEY = "0000000000000005"
NON_NUMERIC_SEARCH_KEY = "12AB"
SELECTED_TRAN_ID = "0000000000000003"
# A row code other than S or s: the invalid-selection literal, then the search (lines 196-201).
INVALID_SELECTION_CODE = "X"
VIEW_SELECTION_CODE = "S"
PAGE_ROWS = 10

# PROCESS-ENTER-KEY lines 199 and 214; STARTBR NOTFND line 608; PROCESS-PF8-KEY line 270 and
# PROCESS-PF7-KEY line 248.
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid value is S"
TRAN_ID_NOT_NUMERIC_MESSAGE = "Tran ID must be Numeric ..."
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
NO_MESSAGE = ""
# A blank TRAN-ORIG-TS, stored as no value with the blank format code.
BLANK_TIMESTAMP_FORMAT = " "
ROW_AMOUNT = Decimal("12.50")


class StubTransactionRepository:
    """TransactionRepository's TRANSACT browses as AsyncMocks; both answer STARTBR NOTFND."""

    def __init__(self) -> None:
        self.BrowseForward = AsyncMock(return_value=None)
        self.BrowseBackward = AsyncMock(return_value=None)

    def ForContext(self, context: OnlineContext) -> "StubTransactionRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


@pytest.fixture(name="transactionRepository")
def TransactionRepositoryStub(monkeypatch) -> StubTransactionRepository:
    """Make cotrn00c build the stub wherever it builds a TransactionRepository; return it."""
    stubRepository = StubTransactionRepository()
    monkeypatch.setattr(cotrn00c, "TransactionRepository", stubRepository.ForContext)
    return stubRepository


def BuildContext() -> OnlineContext:
    """Return an online context with no request or settings, which the stub never reads."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=None)


def BrowseRecords(firstNumber: int, count: int) -> list[dict[str, Any]]:
    """Return ``count`` TRANSACT browse records in key order, from TRAN-ID ``firstNumber``."""
    return [
        {
            "tran_id": f"{number:016d}",
            "tran_desc": f"Purchase {number}",
            "tran_amt": ROW_AMOUNT,
            "orig_ts": None,
            "orig_ts_format": BLANK_TIMESTAMP_FORMAT,
        }
        for number in range(firstNumber, firstNumber + count)
    ]


async def FirstEntry(context: OnlineContext) -> TransactionListResponse:
    """Enter COTRN00 from the main menu: no search key and no paging keys."""
    return await cotrn00c.ListTransactions(context, TransactionListQuery())


async def EnterSearch(context: OnlineContext, searchTranId: str) -> TransactionListResponse:
    """Press ENTER on the page shown with ``searchTranId`` in TRNIDIN and no selection code."""
    query = TransactionListQuery(
        searchTranId=searchTranId, firstKey=SHOWN_FIRST_KEY, page=SHOWN_PAGE
    )
    return await cotrn00c.ListTransactions(context, query)


async def EnterNumericSearch(context: OnlineContext) -> TransactionListResponse:
    """Press ENTER with a numeric search key."""
    return await EnterSearch(context, NUMERIC_SEARCH_KEY)


async def EnterNonNumericSearch(context: OnlineContext) -> TransactionListResponse:
    """Press ENTER with a search key that fails TRNIDINI IS NUMERIC."""
    return await EnterSearch(context, NON_NUMERIC_SEARCH_KEY)


async def EnterSelection(context: OnlineContext, code: str) -> TransactionListResponse:
    """Press ENTER with ``code`` on one row and the numeric search key."""
    request = TransactionSelectionRequest(
        selections=[TransactionSelection(tran_id=SELECTED_TRAN_ID, selection=code)],
        search_tran_id=NUMERIC_SEARCH_KEY,
        first_key=SHOWN_FIRST_KEY,
        page=SHOWN_PAGE,
    )
    return await cotrn00c.ProcessTransactionSelection(context, request)


async def EnterInvalidSelection(context: OnlineContext) -> TransactionListResponse:
    """Press ENTER with an invalid selection code and the numeric search key."""
    return await EnterSelection(context, INVALID_SELECTION_CODE)


async def EnterViewSelection(context: OnlineContext) -> TransactionListResponse:
    """Press ENTER with S on one row, which transfers to the Transaction View."""
    return await EnterSelection(context, VIEW_SELECTION_CODE)


async def PageForward(context: OnlineContext) -> TransactionListResponse:
    """Press F8 after the last key of the first page."""
    query = TransactionListQuery(
        startKey=SHOWN_LAST_KEY, firstKey=SHOWN_FIRST_KEY, direction="forward", page=SHOWN_PAGE
    )
    return await cotrn00c.ListTransactions(context, query)


async def PageBackwardFromSecondPage(context: OnlineContext) -> TransactionListResponse:
    """Press F7 before the first key of the second page."""
    query = TransactionListQuery(
        startKey=SECOND_PAGE_FIRST_KEY, direction="backward", page=SECOND_PAGE
    )
    return await cotrn00c.ListTransactions(context, query)


async def PageBackwardFromFirstPage(context: OnlineContext) -> TransactionListResponse:
    """Press F7 on the first page, which is not above page 1."""
    query = TransactionListQuery(startKey=SHOWN_FIRST_KEY, direction="backward", page=SHOWN_PAGE)
    return await cotrn00c.ListTransactions(context, query)


class KeyPress(NamedTuple):
    """A COTRN00 key, the stub's forward and backward browse answers, and the outcome."""

    pressKey: Callable[[OnlineContext], Awaitable[TransactionListResponse]]
    forwardRecords: list[dict[str, Any]] | None
    backwardRecords: list[dict[str, Any]] | None
    expectedMessage: str
    expectedSearchCleared: bool


# Ten page rows and the look-ahead READNEXT, so no ENDFILE literal replaces the message. F8's
# browse also returns the last key shown first, which its extra READNEXT discards (line 286).
PAGE_WITH_LOOK_AHEAD = BrowseRecords(1, PAGE_ROWS + 1)
SEARCH_PAGE_WITH_LOOK_AHEAD = BrowseRecords(5, PAGE_ROWS + 1)
NEXT_PAGE_AFTER_LAST_KEY = BrowseRecords(10, PAGE_ROWS + 2)
ONLY_LAST_KEY = BrowseRecords(10, 1)
# READPREV order, nearest first: TRAN-IDs 10 down to 1, then the look-ahead 0.
PREVIOUS_PAGE_WITH_LOOK_AHEAD = list(reversed(BrowseRecords(0, PAGE_ROWS + 1)))
SHOWN_PAGE_RECORDS = BrowseRecords(1, PAGE_ROWS)

KEY_PRESSES = [
    KeyPress(FirstEntry, PAGE_WITH_LOOK_AHEAD, None, NO_MESSAGE, True),
    KeyPress(EnterNumericSearch, SEARCH_PAGE_WITH_LOOK_AHEAD, None, NO_MESSAGE, True),
    KeyPress(EnterNumericSearch, None, None, AT_TOP_OF_PAGE_MESSAGE, True),
    KeyPress(EnterNonNumericSearch, SHOWN_PAGE_RECORDS, None, TRAN_ID_NOT_NUMERIC_MESSAGE, False),
    KeyPress(
        EnterInvalidSelection, SEARCH_PAGE_WITH_LOOK_AHEAD, None, INVALID_SELECTION_MESSAGE, True
    ),
    KeyPress(EnterViewSelection, None, None, NO_MESSAGE, False),
    KeyPress(PageForward, NEXT_PAGE_AFTER_LAST_KEY, None, NO_MESSAGE, True),
    KeyPress(PageForward, ONLY_LAST_KEY, None, ALREADY_AT_BOTTOM_MESSAGE, False),
    KeyPress(PageBackwardFromSecondPage, None, PREVIOUS_PAGE_WITH_LOOK_AHEAD, NO_MESSAGE, False),
    KeyPress(PageBackwardFromFirstPage, SHOWN_PAGE_RECORDS, None, ALREADY_AT_TOP_MESSAGE, False),
]
KEY_PRESS_IDS = [
    "first-entry",
    "enter-numeric-found",
    "enter-numeric-startbr-notfnd",
    "enter-non-numeric",
    "enter-invalid-selection-numeric",
    "enter-view-selection",
    "f8-pages-on",
    "f8-refused-at-bottom",
    "f7-pages-back",
    "f7-refused-on-page-1",
]


@pytest.mark.parametrize("keyPress", KEY_PRESSES, ids=KEY_PRESS_IDS)
async def test_search_cleared_reports_trnidin_blanking(transactionRepository, keyPress):
    """``search_cleared`` is true exactly where COTRN00C moves SPACE to TRNIDINO."""
    transactionRepository.BrowseForward.return_value = keyPress.forwardRecords
    transactionRepository.BrowseBackward.return_value = keyPress.backwardRecords
    response = await keyPress.pressKey(BuildContext())
    assert response.message == keyPress.expectedMessage
    assert response.search_cleared is keyPress.expectedSearchCleared
