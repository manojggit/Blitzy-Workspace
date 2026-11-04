"""Unit tests for the COTRN00C (CT00) Transaction List, BMS map COTRN00, over posted records.

cbl/COTRN00C.cbl browses TRANSACT 10 rows per page in key order. The cases post 25 transactions,
TRAN-IDs 0000000000000010 to 0000000000000250 in steps of 10, and compute their expected order as
code-page-037 byte order. Expected values are typed from these lines:
- the first entry, ENTER with no search key, shows the first 10 rows on page 1 (lines 206-207,
    224, 279-328);
- F8 reads on after the last key shown (lines 257-268, 286). The page that ends the file answers
    ``You have reached the bottom of the page...`` (READNEXT ENDFILE, line 642), and F8 on it
    answers ``You are already at the bottom of the page...`` (NEXT-PAGE-NO, line 270);
- F7 reads backward from the first key shown (lines 234-246, 333-376). The page that reaches the
    first record answers ``You have reached the top of the page...`` (READPREV ENDFILE, line
    676), and F7 on page 1 answers ``You are already at the top of the page...`` (line 248);
- a numeric search key starts at the first TRAN-ID at or after it, as page 1 (STARTBR GTEQ,
    lines 209-210, 224); a blank key starts at the lowest key (line 207). A key after every
    TRAN-ID is STARTBR NOTFND, ``You are at the top of the page...`` (line 608), over the page
    shown, with last_key HIGH-VALUES;
- a non-numeric key sets ERR-FLG and ``Tran ID must be Numeric ...`` (line 214): per the
    service's declared contract a 400 with the four standard keys only, so the page shown stays;
- S or s on a row routes to the Transaction View before the search key is read (lines 185-195);
    another code sets ``Invalid selection. Valid value is S`` (line 199) and the list is browsed
    on, a 400 whose body carries the browsed page;
- a TRANSACT file condition on the forward or the backward browse, the batch window's NOTOPEN
    and an IOERR included, is the 503 ``Unable to lookup transaction...`` with a lower-case
    ``t`` (lines 615, 649 and 683).

Every response puts the cursor on the search field (MOVE -1 TO TRNIDINL), and the service writes
nothing: after each call TRANSACT holds exactly the 25 posted records. Each case calls
app.services.transactions.cotrn00c directly with a regular-user context, and restoreSeedState
removes the posted records afterwards.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core import messages
from app.core.errors import FileIoError, LegacyMessageError
from app.core.locks import FileNotOpenError
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.common import Direction
from app.schemas.transactions import (
    TransactionListQuery,
    TransactionListResponse,
    TransactionSelection,
    TransactionSelectionRequest,
)
from app.services.transactions import cotrn00c

REGULAR_USER_TYPE = "U"
# The 25 posted TRAN-IDs: 10 to 250 in steps of 10, so a search key between two of them exists.
POSTED_TRAN_NUMBERS = range(10, 260, 10)
TRAN_ID_DIGITS = 16
EBCDIC_CODE_PAGE = "cp037"
# Every posted record: the seed card (the cards.cards foreign key), purchase type 01, category 1.
POSTED_CARD_NUM = "2988091353094312"
POSTED_TYPE_CD = "01"
POSTED_CAT_CD = 1
POSTED_SOURCE = "POS TERM"
POSTED_AMOUNT = Decimal("1.00")
POSTED_MERCHANT_ID = "000000001"
POSTED_MERCHANT_NAME = "PYTEST MERCHANT"
POSTED_MERCHANT_CITY = "PYTEST CITY"
POSTED_MERCHANT_ZIP = "00000"
# TRAN-ORIG-TS and TRAN-PROC-TS 2022-07-18 10:30:00.000000, the space-separated form ``S``.
POSTED_TIMESTAMP = datetime(2022, 7, 18, 10, 30)
SPACE_SEPARATED_TIMESTAMP_FORMAT = "S"
TRANSACTION_FILE_NAME = "TRANSACT"

INSERT_TRANSACTION = text(
    "INSERT INTO transactions.transactions (tran_id, tran_type_cd, tran_cat_cd, tran_source, "
    "tran_desc, tran_amt, merchant_id, merchant_name, merchant_city, merchant_zip, card_num, "
    "orig_ts, proc_ts, orig_ts_format, proc_ts_format) VALUES (:tranId, :tranTypeCd, "
    ":tranCatCd, :tranSource, :tranDesc, :tranAmt, :merchantId, :merchantName, :merchantCity, "
    ":merchantZip, :cardNum, :origTs, :procTs, :origTsFormat, :procTsFormat)"
)
STORED_TRAN_IDS_QUERY = text("SELECT tran_id FROM transactions.transactions ORDER BY tran_id_sort")

# The ten list rows of the COTRN00 map (SEL0001-SEL0010, TRNID01-TRNID10).
PAGE_ROWS = 10
FIRST_PAGE = 1
SECOND_PAGE_ROWS = slice(10, 20)
# MOVE -1 TO TRNIDINL on every path: the cursor is on the search field.
SEARCH_CURSOR_FIELD = "search_tran_id"
STANDARD_BODY_KEYS = ("message", "info_message", "field_errors", "cursor_field")
EDIT_FAILURE_STATUS = 400
FILE_ERROR_STATUS = 503
# MOVE HIGH-VALUES TO TRAN-ID: the last key of a search that finds no TRAN-ID at or after it.
HIGH_VALUES_KEY = "\xff" * 16
VIEW_ROUTE_PREFIX = "/transactions/view?tranId="
# TRNIDIN keys: one after every posted TRAN-ID, and one IS NUMERIC rejects (line 209).
UNMATCHED_SEARCH_KEY = "9999999999999999"
NON_NUMERIC_SEARCH_KEY = "ABC"
SELECTED_ROW_INDEX = 3

# The COTRN00C literals: PROCESS-PF7-KEY line 248, PROCESS-PF8-KEY line 270, STARTBR NOTFND line
# 608, READNEXT ENDFILE line 642, READPREV ENDFILE line 676.
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
REACHED_TOP_MESSAGE = "You have reached the top of the page..."
# PROCESS-ENTER-KEY lines 199 and 214; the STARTBR, READNEXT and READPREV WHEN OTHER, line 615.
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid value is S"
TRAN_ID_NOT_NUMERIC_MESSAGE = "Tran ID must be Numeric ..."
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup transaction..."
# MOVE SPACES TO WS-MESSAGE: a page no paging literal is set on has a blank message line.
NO_MESSAGE = ""

SHARED_PAGING_LITERALS = {
    "ALREADY_AT_TOP_MESSAGE": ALREADY_AT_TOP_MESSAGE,
    "ALREADY_AT_BOTTOM_MESSAGE": ALREADY_AT_BOTTOM_MESSAGE,
    "AT_TOP_OF_PAGE_MESSAGE": AT_TOP_OF_PAGE_MESSAGE,
    "REACHED_BOTTOM_MESSAGE": REACHED_BOTTOM_MESSAGE,
    "REACHED_TOP_MESSAGE": REACHED_TOP_MESSAGE,
}


class PagingStep(NamedTuple):
    """One paging key pressed on the page shown, and the screen it must leave."""

    buildQuery: Callable[[TransactionListResponse], TransactionListQuery]
    rows: slice
    page: int
    message: str


class KeyedSearch(NamedTuple):
    """A TRNIDIN search key and the index, in the posted order, of the first row it shows."""

    searchTranId: str
    firstRowIndex: int


class RowSelection(NamedTuple):
    """A SELnnnn code keyed on one row of the page shown, with the TRNIDIN text beside it."""

    rowIndex: int
    code: str
    searchTranId: str = ""


def EbcdicKey(tranId: str) -> bytes:
    """Return the code-page-037 bytes of a TRAN-ID, the VSAM key order of TRANSACT."""
    return tranId.encode(EBCDIC_CODE_PAGE)


def BuildTransactionRow(tranId: str) -> dict[str, object]:
    """Return the bound INSERT parameters of one posted TRAN-RECORD with TRAN-ID ``tranId``."""
    return {
        "tranId": tranId,
        "tranTypeCd": POSTED_TYPE_CD,
        "tranCatCd": POSTED_CAT_CD,
        "tranSource": POSTED_SOURCE,
        "tranDesc": f"PYTEST PURCHASE {tranId}",
        "tranAmt": POSTED_AMOUNT,
        "merchantId": POSTED_MERCHANT_ID,
        "merchantName": POSTED_MERCHANT_NAME,
        "merchantCity": POSTED_MERCHANT_CITY,
        "merchantZip": POSTED_MERCHANT_ZIP,
        "cardNum": POSTED_CARD_NUM,
        "origTs": POSTED_TIMESTAMP,
        "procTs": POSTED_TIMESTAMP,
        "origTsFormat": SPACE_SEPARATED_TIMESTAMP_FORMAT,
        "procTsFormat": SPACE_SEPARATED_TIMESTAMP_FORMAT,
    }


@pytest.fixture(name="postedTransactions")
def PostedTransactions(restoreSeedState: None, syncEngine: Engine) -> list[str]:
    """Commit the 25 posted transactions; return their TRAN-IDs in EBCDIC key order.

    restoreSeedState truncates them again at teardown, leaving TRANSACT empty as seeded.
    """
    tranIds = [str(number).zfill(TRAN_ID_DIGITS) for number in POSTED_TRAN_NUMBERS]
    with syncEngine.begin() as connection:
        for tranId in tranIds:
            connection.execute(INSERT_TRANSACTION, BuildTransactionRow(tranId))
    return sorted(tranIds, key=EbcdicKey)


def StoredTranIds(syncEngine: Engine) -> list[str]:
    """Return every committed TRAN-ID in EBCDIC key order, to show the service wrote nothing."""
    with syncEngine.connect() as connection:
        return list(connection.execute(STORED_TRAN_IDS_QUERY).scalars())


def RowIds(response: TransactionListResponse) -> list[str]:
    """Return the TRNIDnn values of the rows a list response shows, in screen order."""
    return [row.tran_id for row in response.rows]


def ScreenOutcome(response: TransactionListResponse) -> tuple[list[str], int, str, str | None]:
    """Return the rows, page number, ERRMSG literal and cursor field of a list response."""
    return (RowIds(response), response.page, response.message, response.cursor_field)


def StandardBody(message: str) -> dict[str, object]:
    """Return the four standard keys of an error body, with the cursor on the search field."""
    return {
        "message": message,
        "info_message": "",
        "field_errors": [SEARCH_CURSOR_FIELD],
        "cursor_field": SEARCH_CURSOR_FIELD,
    }


def F8Query(shownPage: TransactionListResponse) -> TransactionListQuery:
    """Return F8 on the page shown: forward after its last key, with its first key and page."""
    return TransactionListQuery(
        startKey=shownPage.last_key,
        firstKey=shownPage.first_key,
        direction=Direction.FORWARD,
        page=shownPage.page,
    )


def F7Query(shownPage: TransactionListResponse) -> TransactionListQuery:
    """Return F7 on the page shown: backward before its first key, with its page number."""
    return TransactionListQuery(
        startKey=shownPage.first_key, direction=Direction.BACKWARD, page=shownPage.page
    )


def SearchQuery(shownPage: TransactionListResponse, searchTranId: str) -> TransactionListQuery:
    """Return ENTER on the page shown with ``searchTranId`` keyed in TRNIDIN."""
    return TransactionListQuery(
        searchTranId=searchTranId, firstKey=shownPage.first_key, page=shownPage.page
    )


def SelectionRequest(
    shownPage: TransactionListResponse, rowSelection: RowSelection
) -> TransactionSelectionRequest:
    """Return ENTER on the page shown with one row's code keyed and every other code blank."""
    selections = [
        TransactionSelection(
            tran_id=tranId, selection=rowSelection.code if index == rowSelection.rowIndex else ""
        )
        for index, tranId in enumerate(RowIds(shownPage))
    ]
    return TransactionSelectionRequest(
        selections=selections,
        search_tran_id=rowSelection.searchTranId,
        first_key=shownPage.first_key,
        page=shownPage.page,
    )


async def ShowFirstPage(context) -> TransactionListResponse:
    """Enter the list from the menu: no search key and no paging keys (lines 112-116)."""
    return await cotrn00c.ListTransactions(context, TransactionListQuery())


async def ShowSecondPage(context) -> TransactionListResponse:
    """Enter the list and press F8 once: the second page, posted rows 11 to 20."""
    firstPage = await ShowFirstPage(context)
    return await cotrn00c.ListTransactions(context, F8Query(firstPage))


PAGING_WALK = (
    PagingStep(F8Query, SECOND_PAGE_ROWS, 2, NO_MESSAGE),
    PagingStep(F8Query, slice(20, 25), 3, REACHED_BOTTOM_MESSAGE),
    PagingStep(F8Query, slice(20, 25), 3, ALREADY_AT_BOTTOM_MESSAGE),
    PagingStep(F7Query, SECOND_PAGE_ROWS, 2, NO_MESSAGE),
    PagingStep(F7Query, slice(0, PAGE_ROWS), FIRST_PAGE, REACHED_TOP_MESSAGE),
    PagingStep(F7Query, slice(0, PAGE_ROWS), FIRST_PAGE, ALREADY_AT_TOP_MESSAGE),
)
KEYED_SEARCHES = [
    KeyedSearch("0000000000000050", 4),
    KeyedSearch("0000000000000015", 1),
    KeyedSearch("", 0),
]
KEYED_SEARCH_IDS = ["exact-match", "between-keys", "blank-key"]
VIEW_SELECTIONS = [
    RowSelection(SELECTED_ROW_INDEX, "S"),
    RowSelection(SELECTED_ROW_INDEX, "s"),
    RowSelection(SELECTED_ROW_INDEX, "S", "0000000000000150"),
]
VIEW_SELECTION_IDS = ["upper-case-S", "lower-case-s", "S-with-search-key"]
INVALID_SELECTION = RowSelection(SELECTED_ROW_INDEX, "X")


@pytest.mark.parametrize(
    ("constantName", "expectedLiteral"),
    SHARED_PAGING_LITERALS.items(),
    ids=list(SHARED_PAGING_LITERALS),
)
def test_shared_paging_constants_equal_the_cotrn00c_literals(constantName, expectedLiteral):
    """app.core.messages holds the five paging literals exactly as COTRN00C moves them."""
    assert getattr(messages, constantName) == expectedLiteral


async def test_first_entry_shows_the_first_ten_transactions_on_page_one(
    makeOnlineContext, postedTransactions, syncEngine
):
    """No search key starts at the lowest key; a look-ahead record leaves no paging literal."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    firstPage = await ShowFirstPage(context)
    expectedRows = postedTransactions[:PAGE_ROWS]
    assert ScreenOutcome(firstPage) == (expectedRows, FIRST_PAGE, NO_MESSAGE, SEARCH_CURSOR_FIELD)
    assert (firstPage.first_key, firstPage.last_key) == (expectedRows[0], expectedRows[-1])
    assert StoredTranIds(syncEngine) == postedTransactions


async def test_f8_and_f7_page_through_the_file_with_each_paging_literal(
    makeOnlineContext, postedTransactions, syncEngine
):
    """F8 to the end of the file and past it, then F7 back to page 1 and before it."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    shownPage = await ShowFirstPage(context)
    for step in PAGING_WALK:
        shownPage = await cotrn00c.ListTransactions(context, step.buildQuery(shownPage))
        expectedScreen = (postedTransactions[step.rows], step.page, step.message)
        assert ScreenOutcome(shownPage) == (*expectedScreen, SEARCH_CURSOR_FIELD), step
    assert StoredTranIds(syncEngine) == postedTransactions


@pytest.mark.parametrize("keyedSearch", KEYED_SEARCHES, ids=KEYED_SEARCH_IDS)
async def test_search_from_the_second_page_starts_at_the_first_key_at_or_after_it(
    makeOnlineContext, postedTransactions, syncEngine, keyedSearch
):
    """STARTBR GTEQ is inclusive, a blank key is LOW-VALUES, and the page number resets to 1."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    secondPage = await ShowSecondPage(context)
    query = SearchQuery(secondPage, keyedSearch.searchTranId)
    searchedPage = await cotrn00c.ListTransactions(context, query)
    firstRow = keyedSearch.firstRowIndex
    expectedRows = postedTransactions[firstRow : firstRow + PAGE_ROWS]
    assert ScreenOutcome(searchedPage) == (
        expectedRows,
        FIRST_PAGE,
        NO_MESSAGE,
        SEARCH_CURSOR_FIELD,
    )
    assert StoredTranIds(syncEngine) == postedTransactions


async def test_search_after_every_key_answers_top_of_page_over_the_page_shown(
    makeOnlineContext, postedTransactions, syncEngine
):
    """STARTBR NOTFND keeps the second page's rows on page 1, with last_key HIGH-VALUES."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    secondPage = await ShowSecondPage(context)
    query = SearchQuery(secondPage, UNMATCHED_SEARCH_KEY)
    notFoundPage = await cotrn00c.ListTransactions(context, query)
    assert ScreenOutcome(notFoundPage) == (
        postedTransactions[SECOND_PAGE_ROWS],
        FIRST_PAGE,
        AT_TOP_OF_PAGE_MESSAGE,
        SEARCH_CURSOR_FIELD,
    )
    assert notFoundPage.last_key == HIGH_VALUES_KEY
    assert StoredTranIds(syncEngine) == postedTransactions


async def test_non_numeric_search_is_an_edit_failure_that_leaves_the_page_shown(
    makeOnlineContext, postedTransactions, syncEngine
):
    """ERR-FLG rebuilds no page: a 400 with the four standard keys only, so the rows stay."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    secondPage = await ShowSecondPage(context)
    with pytest.raises(LegacyMessageError) as raised:
        await cotrn00c.ListTransactions(context, SearchQuery(secondPage, NON_NUMERIC_SEARCH_KEY))
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    assert raised.value.Body() == StandardBody(TRAN_ID_NOT_NUMERIC_MESSAGE)
    assert RowIds(secondPage) == postedTransactions[SECOND_PAGE_ROWS]
    assert StoredTranIds(syncEngine) == postedTransactions


@pytest.mark.parametrize("rowSelection", VIEW_SELECTIONS, ids=VIEW_SELECTION_IDS)
async def test_s_selection_routes_to_the_transaction_view_before_any_search(
    makeOnlineContext, postedTransactions, syncEngine, rowSelection
):
    """S or s ends the request with the view route; the search key is never applied."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    firstPage = await ShowFirstPage(context)
    request = SelectionRequest(firstPage, rowSelection)
    selected = await cotrn00c.ProcessTransactionSelection(context, request)
    selectedTranId = postedTransactions[rowSelection.rowIndex]
    assert selected.route == VIEW_ROUTE_PREFIX + selectedTranId
    assert (selected.rows, selected.message, selected.search_cleared) == ([], NO_MESSAGE, False)
    assert StoredTranIds(syncEngine) == postedTransactions


async def test_invalid_selection_code_answers_the_literal_with_the_browsed_list(
    makeOnlineContext, postedTransactions, syncEngine
):
    """A code other than S keeps its literal over the re-browsed first page: a 400 with rows."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    firstPage = await ShowFirstPage(context)
    request = SelectionRequest(firstPage, INVALID_SELECTION)
    with pytest.raises(LegacyMessageError) as raised:
        await cotrn00c.ProcessTransactionSelection(context, request)
    refusalBody = raised.value.Body()
    assert raised.value.httpStatus == EDIT_FAILURE_STATUS
    standardKeys = {key: refusalBody[key] for key in STANDARD_BODY_KEYS}
    assert standardKeys == StandardBody(INVALID_SELECTION_MESSAGE)
    listedIds = [row["tran_id"] for row in refusalBody["rows"]]
    assert (listedIds, refusalBody["page"]) == (postedTransactions[:PAGE_ROWS], FIRST_PAGE)
    assert StoredTranIds(syncEngine) == postedTransactions


async def test_transact_file_condition_answers_unable_to_lookup_transaction(
    makeOnlineContext, postedTransactions, syncEngine, monkeypatch
):
    """A NOTOPEN on the TRANSACT browse is the WHEN OTHER literal with status 503."""
    notOpenBrowse = AsyncMock(side_effect=FileNotOpenError(TRANSACTION_FILE_NAME))
    monkeypatch.setattr(TransactionRepository, "BrowseForward", notOpenBrowse)
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    with pytest.raises(LegacyMessageError) as raised:
        await ShowFirstPage(context)
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == StandardBody(TRAN_LOOKUP_FAILED_MESSAGE)
    notOpenBrowse.assert_awaited_once()
    assert StoredTranIds(syncEngine) == postedTransactions


@pytest.mark.usefixtures("postedTransactions")
@pytest.mark.parametrize("conditionType", [FileIoError, FileNotOpenError], ids=["ioerr", "notopen"])
async def test_transact_file_condition_on_the_backward_browse_answers_unable_to_lookup_transaction(
    makeOnlineContext, syncEngine, monkeypatch, conditionType
):
    """IOERR, or the batch window's NOTOPEN, on F7's STARTBR/READPREV browse is a 503."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    secondPage = await ShowSecondPage(context)
    storedTranIds = StoredTranIds(syncEngine)
    backwardBrowse = AsyncMock(side_effect=conditionType(TRANSACTION_FILE_NAME))
    monkeypatch.setattr(TransactionRepository, "BrowseBackward", backwardBrowse)
    with pytest.raises(LegacyMessageError) as raised:
        await cotrn00c.ListTransactions(context, F7Query(secondPage))
    assert RowIds(secondPage) == storedTranIds[SECOND_PAGE_ROWS]
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == StandardBody(TRAN_LOOKUP_FAILED_MESSAGE)
    backwardBrowse.assert_awaited_once_with(secondPage.first_key, limit=PAGE_ROWS)
    assert StoredTranIds(syncEngine) == storedTranIds
