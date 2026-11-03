"""Unit tests for the HTTP status of the COTRN00C (CT00) edit failures on the transaction list.

cbl/COTRN00C.cbl PROCESS-ENTER-KEY processes a selection code first: S or s transfers to
COTRN01C (lines 185-195), and any other code sets ``Invalid selection. Valid value is S`` and
processing continues (lines 196-201). A non-numeric TRNIDIN then sets ERR-FLG and
``Tran ID must be Numeric ...`` (lines 209-217), so PROCESS-PAGE-FORWARD rebuilds no page (line
283) and the list is left unchanged; only on an empty TRANSACT does the STARTBR it still issues
answer NOTFND, whose ``You are at the top of the page...`` replaces the literal (lines
605-611). Otherwise the browse runs from the search key, and an ENDFILE before the look-ahead
READNEXT (``You have reached the bottom of the page...``, lines 639-645) or a STARTBR NOTFND
replaces the invalid-selection literal. An edit failure is a 400 whose body carries the four
standard keys; the invalid-selection one adds the browsed page's list keys. Expected values
are typed from those lines:
- a non-numeric search key answers 400 with the four standard keys only, from the list and the
    selection routes, an invalid code or none, after one existence probe and no re-read;
- on an empty TRANSACT it answers 200 with the top-of-page literal on an empty page 0;
- an invalid code the browse leaves on ERRMSG answers 400 with the page's rows, page, first_key,
    last_key and search_cleared, each amount a JSON decimal string; the code sets no ERR-FLG, so
    the browse blanks TRNIDIN (line 325) and search_cleared is true;
- an invalid code a paging literal replaces answers 200 with that page;
- S and s still route to the Transaction View without any read, even with a non-numeric key.

app.services.transactions.cotrn00c's TransactionRepository is replaced by a stub whose browses
are AsyncMocks, so no database is needed.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, call

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.schemas.transactions import (
    TransactionListQuery,
    TransactionListResponse,
    TransactionSelection,
    TransactionSelectionRequest,
)
from app.services.transactions import cotrn00c

# The ten list rows of the COTRN00 map (SEL0001-SEL0010).
PAGE_ROWS = 10
# The re-read of a shown page asks for 9 rows and the look-ahead: 10 records in all.
SHOWN_PAGE_READ_LIMIT = PAGE_ROWS - 1
# The STARTBR at LOW-VALUES that probes whether TRANSACT holds any record reads no row.
EXISTENCE_PROBE_LIMIT = 0
# TRNIDIN values IS NUMERIC rejects: letters, and digits BMS pads with spaces to 16 positions.
LETTERS_SEARCH_KEY = "ABC"
SHORT_DIGITS_SEARCH_KEY = "123"
# No TRAN-ID is at or after this numeric key, so the stub answers its STARTBR with NOTFND.
UNMATCHED_SEARCH_KEY = "9999999999999999"
BLANK_SEARCH_KEY = ""
# LOW-VALUES: the browse key of a blank search (line 207), which the stub receives as None.
LOW_VALUES_KEY = None
# MOVE HIGH-VALUES TO TRAN-ID: the last key of a search that finds nothing.
HIGH_VALUES_KEY = "\xff" * 16
NO_KEY = ""
# The page shown when ENTER is pressed: its first key, its number and the selected row.
SHOWN_FIRST_NUMBER = 1
SHOWN_FIRST_KEY = "0000000000000001"
SHOWN_LAST_KEY = "0000000000000010"
SHOWN_PAGE = 3
SELECTED_TRAN_ID = "0000000000000003"
# A row code other than S or s; U is CU00's code, invalid on COTRN00.
INVALID_SELECTION_CODE = "X"
USER_UPDATE_CODE = "U"
# The records at or after the search key when the page ends the file.
END_OF_FILE_ROWS = 5
END_OF_FILE_LAST_KEY = "0000000000000005"

# The selection, search, STARTBR NOTFND and ENDFILE literals (lines 199, 214, 608, 642).
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid value is S"
TRAN_ID_NOT_NUMERIC_MESSAGE = "Tran ID must be Numeric ..."
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
# Every path ends with MOVE -1 TO TRNIDINL: the cursor is on the search field.
SEARCH_CURSOR_FIELD = "search_tran_id"
EDIT_FAILURE_STATUS = 400
ENTER_PAGE = 0
FIRST_PAGE = 1
# TRAN-ORIG-TS 2022-07-18 10:30:00.000000 in the space-separated form ``S``: TDATEnn MM/DD/YY.
ORIG_TS = datetime(2022, 7, 18, 10, 30)
ORIG_TS_FORMAT = "S"
ROW_DATE = "07/18/22"


class StubTransactionRepository:
    """TransactionRepository's TRANSACT browses as AsyncMocks; both answer NOTFND by default."""

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


def StandardBody(message: str) -> dict[str, object]:
    """Return the four standard keys of a 400 body, with the cursor on the search field."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [SEARCH_CURSOR_FIELD],
        "cursor_field": SEARCH_CURSOR_FIELD,
    }


def BrowseRecords(firstNumber: int, count: int) -> list[dict[str, object]]:
    """Return ``count`` TRANSACT browse records in key order, from TRAN-ID <firstNumber>."""
    return [
        {
            "tran_id": f"{number:016d}",
            "orig_ts": ORIG_TS,
            "orig_ts_format": ORIG_TS_FORMAT,
            "tran_desc": f"Purchase {number}",
            "tran_amt": Decimal(f"-{number}.25"),
        }
        for number in range(firstNumber, firstNumber + count)
    ]


def ExpectedRows(firstNumber: int, count: int) -> list[dict[str, str]]:
    """Return the JSON list rows of those records: TRNIDnn, TDATEnn, TDESCnn and TAMTnnn."""
    return [
        {
            "tran_id": f"{number:016d}",
            "tran_date": ROW_DATE,
            "tran_desc": f"Purchase {number}",
            "tran_amt": f"-{number}.25",
        }
        for number in range(firstNumber, firstNumber + count)
    ]


def SelectionRequest(code: str, searchKey: str) -> TransactionSelectionRequest:
    """Return ENTER on page 3 with ``code`` on the selected row, or no code when it is blank."""
    selections = [TransactionSelection(tran_id=SELECTED_TRAN_ID, selection=code)] if code else []
    return TransactionSelectionRequest(
        selections=selections, search_tran_id=searchKey, first_key=SHOWN_FIRST_KEY, page=SHOWN_PAGE
    )


async def ListSearch(context: OnlineContext, searchKey: str) -> TransactionListResponse:
    """Press ENTER on COTRN00 through the list route, with the shown page's first key."""
    query = TransactionListQuery(searchTranId=searchKey, firstKey=SHOWN_FIRST_KEY, page=SHOWN_PAGE)
    return await cotrn00c.ListTransactions(context, query)


async def SelectWithInvalidCode(context: OnlineContext, searchKey: str) -> TransactionListResponse:
    """Press ENTER with an invalid selection code on one row of the shown page."""
    request = SelectionRequest(INVALID_SELECTION_CODE, searchKey)
    return await cotrn00c.ProcessTransactionSelection(context, request)


async def SelectWithNoCode(context: OnlineContext, searchKey: str) -> TransactionListResponse:
    """Press ENTER through the selection route with no selection code."""
    return await cotrn00c.ProcessTransactionSelection(context, SelectionRequest("", searchKey))


class SearchPress(NamedTuple):
    """An ENTER on COTRN00 and the search key it is pressed with."""

    pressEnter: Callable[[OnlineContext, str], Awaitable[TransactionListResponse]]
    searchKey: str


NON_NUMERIC_SEARCHES = [
    SearchPress(ListSearch, LETTERS_SEARCH_KEY),
    SearchPress(ListSearch, SHORT_DIGITS_SEARCH_KEY),
    SearchPress(SelectWithInvalidCode, LETTERS_SEARCH_KEY),
    SearchPress(SelectWithNoCode, LETTERS_SEARCH_KEY),
]
NON_NUMERIC_SEARCH_IDS = ["list-letters", "list-short-digits", "selection-X", "selection-none"]


@pytest.mark.parametrize("searchPress", NON_NUMERIC_SEARCHES, ids=NON_NUMERIC_SEARCH_IDS)
async def test_non_numeric_search_is_a_four_key_edit_failure(transactionRepository, searchPress):
    """ERR-FLG leaves the list unchanged: 400 with the four keys only, and no page re-read."""
    transactionRepository.BrowseForward.return_value = BrowseRecords(SHOWN_FIRST_NUMBER, 1)
    with pytest.raises(LegacyMessageError) as raised:
        await searchPress.pressEnter(BuildContext(), searchPress.searchKey)
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == StandardBody(TRAN_ID_NOT_NUMERIC_MESSAGE)
    transactionRepository.BrowseForward.assert_awaited_once_with(
        LOW_VALUES_KEY, inclusive=True, limit=EXISTENCE_PROBE_LIMIT
    )
    transactionRepository.BrowseBackward.assert_not_awaited()


@pytest.mark.parametrize(
    "pressEnter", [ListSearch, SelectWithInvalidCode], ids=["list", "selection-X"]
)
async def test_non_numeric_search_on_an_empty_file_answers_top_of_page(
    transactionRepository, pressEnter
):
    """STARTBR NOTFND replaces the literal: 200 with the top literal, page 0 and no rows."""
    emptyPage = await pressEnter(BuildContext(), LETTERS_SEARCH_KEY)
    assert emptyPage.message == AT_TOP_OF_PAGE_MESSAGE
    assert (emptyPage.page, emptyPage.rows) == (ENTER_PAGE, [])
    assert (emptyPage.first_key, emptyPage.last_key) == (NO_KEY, NO_KEY)
    assert emptyPage.cursor_field == SEARCH_CURSOR_FIELD
    transactionRepository.BrowseForward.assert_awaited_once_with(
        LOW_VALUES_KEY, inclusive=True, limit=EXISTENCE_PROBE_LIMIT
    )


@pytest.mark.parametrize(
    ("searchKey", "browseKey"),
    [(BLANK_SEARCH_KEY, LOW_VALUES_KEY), (SHOWN_FIRST_KEY, SHOWN_FIRST_KEY)],
    ids=["blank-search", "keyed-search"],
)
@pytest.mark.parametrize("code", [INVALID_SELECTION_CODE, USER_UPDATE_CODE])
async def test_invalid_selection_on_a_page_with_a_next_page_is_an_edit_failure(
    transactionRepository, code, searchKey, browseKey
):
    """The literal stays: 400 with the four keys plus the JSON page and its TRNIDIN blanking."""
    records = BrowseRecords(SHOWN_FIRST_NUMBER, PAGE_ROWS + 1)
    transactionRepository.BrowseForward.return_value = records
    with pytest.raises(LegacyMessageError) as raised:
        await cotrn00c.ProcessTransactionSelection(
            BuildContext(), SelectionRequest(code, searchKey)
        )
    pageKeys = {"rows": ExpectedRows(SHOWN_FIRST_NUMBER, PAGE_ROWS), "page": FIRST_PAGE}
    pageKeys |= {"first_key": SHOWN_FIRST_KEY, "last_key": SHOWN_LAST_KEY}
    pageKeys |= {"search_cleared": True}
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == StandardBody(INVALID_SELECTION_MESSAGE) | pageKeys
    transactionRepository.BrowseForward.assert_awaited_once_with(
        browseKey, inclusive=True, limit=PAGE_ROWS
    )


class ReplacedLiteral(NamedTuple):
    """A search after an invalid code, the browses it reads, and the literal and page left."""

    searchKey: str
    browseResults: list[list[dict[str, object]] | None]
    expectedBrowses: list[object]
    expectedMessage: str
    expectedRowCount: int
    expectedLastKey: str


REPLACED_LITERALS = [
    ReplacedLiteral(
        BLANK_SEARCH_KEY,
        [BrowseRecords(SHOWN_FIRST_NUMBER, END_OF_FILE_ROWS)],
        [call(LOW_VALUES_KEY, inclusive=True, limit=PAGE_ROWS)],
        REACHED_BOTTOM_MESSAGE,
        END_OF_FILE_ROWS,
        END_OF_FILE_LAST_KEY,
    ),
    ReplacedLiteral(
        UNMATCHED_SEARCH_KEY,
        [None, BrowseRecords(SHOWN_FIRST_NUMBER, PAGE_ROWS)],
        [
            call(UNMATCHED_SEARCH_KEY, inclusive=True, limit=PAGE_ROWS),
            call(SHOWN_FIRST_KEY, inclusive=True, limit=SHOWN_PAGE_READ_LIMIT),
        ],
        AT_TOP_OF_PAGE_MESSAGE,
        PAGE_ROWS,
        HIGH_VALUES_KEY,
    ),
]
REPLACED_LITERAL_IDS = ["page-ends-file", "search-finds-nothing"]


@pytest.mark.parametrize("replaced", REPLACED_LITERALS, ids=REPLACED_LITERAL_IDS)
async def test_invalid_selection_replaced_by_a_paging_literal_answers_the_page(
    transactionRepository, replaced
):
    """ENDFILE or STARTBR NOTFND overwrites the literal: a 200 page with the paging literal."""
    transactionRepository.BrowseForward.side_effect = replaced.browseResults
    request = SelectionRequest(INVALID_SELECTION_CODE, replaced.searchKey)
    listPage = await cotrn00c.ProcessTransactionSelection(BuildContext(), request)
    shownRows = [row.model_dump(mode="json") for row in listPage.rows]
    assert shownRows == ExpectedRows(SHOWN_FIRST_NUMBER, replaced.expectedRowCount)
    assert (listPage.message, listPage.route) == (replaced.expectedMessage, None)
    assert (listPage.first_key, listPage.last_key) == (SHOWN_FIRST_KEY, replaced.expectedLastKey)
    assert transactionRepository.BrowseForward.await_args_list == replaced.expectedBrowses


@pytest.mark.parametrize(
    ("code", "searchKey"),
    [("S", BLANK_SEARCH_KEY), ("s", LETTERS_SEARCH_KEY)],
    ids=["S-blank-search", "s-non-numeric-search"],
)
async def test_select_code_routes_to_the_transaction_view_without_any_read(
    transactionRepository, code, searchKey
):
    """S and s end the request as XCTL COTRN01C did, before the search key is examined."""
    selected = await cotrn00c.ProcessTransactionSelection(
        BuildContext(), SelectionRequest(code, searchKey)
    )
    assert selected.route == f"/transactions/view?tranId={SELECTED_TRAN_ID}"
    assert (selected.rows, selected.message) == ([], "")
    transactionRepository.BrowseForward.assert_not_awaited()
    transactionRepository.BrowseBackward.assert_not_awaited()
