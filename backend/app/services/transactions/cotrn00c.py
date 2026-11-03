"""Replaces: COTRN00C (CT00), BMS COTRN00, copybooks CVTRA05Y.

The transaction list: TRANSACT records 10 rows per page in EBCDIC key order, browsed from the
TRNIDIN search key with ENTER, paged with F8 and F7, and the S row selection that opens the
Transaction View (COTRN01C).

Keys. The COMMAREA paging state CDEMO-CT00-INFO comes back from the client: ``page`` is
CDEMO-CT00-PAGE-NUM, ``startKey`` the first key shown (F7) or the last key shown (F8), and
``firstKey`` (query) or ``first_key`` (body) the first key shown. Direction backward is F7;
forward with a startKey is F8; forward with an empty startKey is ENTER, which is also the first
entry from the menu. Every POST /transactions/selection is ENTER.

NEXT-PAGE-FLG is derived, not carried: F8 pages on only when a record follows the last key
shown, which holds after a forward page that ended with its look-ahead READNEXT and after every
backward page; otherwise F8 answers ``You are already at the bottom of the page...``. A search
that finds nothing sets NEXT-PAGE-NO over a page it leaves in place, so its response carries
last_key HIGH-VALUES, after which no record follows.

Rows not rebuilt. Where COTRN00C re-sends the received map with only ERRMSG changed (F7 on page
1, F8 with no page after the last key, a non-numeric search, a STARTBR NOTFND), the service
re-reads the page shown from its first key, because every screen outcome is a 200 list
response that replaces the client's rows. An F8 sent without ``firstKey`` re-reads the 10
records ending at the last key shown instead.

Every list response puts the cursor on search_tran_id (MOVE -1 TO TRNIDINL on every path); the
map shows ERRMSG in red. Only a TRANSACT file condition, the batch window's NOTOPEN included,
is 503 ``Unable to lookup transaction...``. The program blanks TRNIDIN whenever the body of
PROCESS-PAGE-FORWARD runs (lines 228, 325): after ENTER with a blank or numeric key, STARTBR
NOTFND included, and after an F8 that pages on; the response says so in ``search_cleared``. It
keeps TRNIDIN after a non-numeric search, every F7 and an F8 refusal.

Legacy paths with no code here: EIBCALEN = 0 (back to sign-on) is the RequireRegular session
guard; PF3 (XCTL COMEN01C) is frontend navigation; other AID keys cannot reach the server;
POPULATE-HEADER-INFO is the frontend ScreenShell. A STARTBR NOTFND ends with its own literal:
the READNEXT or READPREV the program still issues after it, and its ENDBR, find no browse
started (INVREQ), and that abend path is not reproduced.
"""

import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from app.core.dependencies import OnlineContext
from app.core.errors import HTTP_FILE_ERROR, LegacyMessageError, LogFileCondition, RepositoryError
from app.core.messages import (
    ALREADY_AT_BOTTOM_MESSAGE,
    ALREADY_AT_TOP_MESSAGE,
    AT_TOP_OF_PAGE_MESSAGE,
    REACHED_BOTTOM_MESSAGE,
    REACHED_TOP_MESSAGE,
)
from app.repositories.transaction_repository import HIGH_VALUES_TRAN_ID, TransactionRepository
from app.schemas.common import Direction
from app.schemas.transactions import (
    TRANSACTION_LIST_PAGE_SIZE,
    TransactionListQuery,
    TransactionListResponse,
    TransactionListRow,
    TransactionSelection,
    TransactionSelectionRequest,
    TransactionSelectionResponse,
)
from app.utils.decimal_utils import StoreInto
from app.utils.record_layouts import EncodeTimestamp

LOGGER = logging.getLogger(__name__)

# WS-TRANID (line 37).
TRANSACTION_ID = "CT00"
# The CICS commands of the TRANSACT browses, which one repository call runs, as their logged
# file conditions name them; a STARTBR alone tests whether TRANSACT holds any record.
STARTBR_OPERATION = "STARTBR"
BROWSE_FORWARD_OPERATION = "STARTBR/READNEXT"
BROWSE_BACKWARD_OPERATION = "STARTBR/READPREV"

# The ten list rows of the COTRN00 map: SEL0001-SEL0010 with TRNID01-TRNID10.
PAGE_SIZE = TRANSACTION_LIST_PAGE_SIZE
# TDESCnn LENGTH=26 (bms/COTRN00.bms) receives TRAN-DESC X(100).
ROW_DESCRIPTION_LENGTH = 26
# WS-TRAN-AMT PIC +99999999.99 (line 56): MOVE TRAN-AMT keeps 8 integer and 2 decimal digits.
AMOUNT_INTEGER_DIGITS = 8
AMOUNT_DECIMAL_DIGITS = 2
# CDEMO-CT00-PAGE-NUM PIC 9(08): ADD 1 keeps the low-order eight digits.
PAGE_NUMBER_MODULUS = 10**8
# TRNIDIN is X(16); IS NUMERIC tests every position of the space-padded field (line 209).
SEARCH_KEY_LENGTH = 16
NUMERIC_CHARACTERS = frozenset("0123456789")
BLANK_CHARACTER = " "
# EVALUATE CDEMO-CT00-TRN-SEL-FLG WHEN 'S' WHEN 's' (lines 185-187).
SELECT_CODES = ("S", "s")
# The frontend page of XCTL COTRN01C (lines 188-195), keyed by tranId.
VIEW_ROUTE = "/transactions/view"
ROUTE_TRAN_ID_PARAMETER = "tranId"
# Every path ends with MOVE -1 TO TRNIDINL: the cursor goes to the search field.
SEARCH_CURSOR_FIELD = "search_tran_id"
# PROCESS-ENTER-KEY line 199.
INVALID_SELECTION_MESSAGE = "Invalid selection. Valid value is S"
# PROCESS-ENTER-KEY line 214.
TRAN_ID_NOT_NUMERIC_MESSAGE = "Tran ID must be Numeric ..."
# The WHEN OTHER branches of STARTBR-, READNEXT- and READPREV-TRANSACT-FILE (lines 615, 649,
# 683), lower-case "t" as in the source; the batch window's NOTOPEN reaches them too.
TRAN_LOOKUP_FAILED_MESSAGE = "Unable to lookup transaction..."
LOOKUP_ERROR_STATUS = HTTP_FILE_ERROR
# MOVE SPACES TO WS-MESSAGE (MAIN-PARA line 102): a page with no literal has a blank line.
NO_MESSAGE = ""
# PROCESS-ENTER-KEY moves 0 to CDEMO-CT00-PAGE-NUM before PROCESS-PAGE-FORWARD (line 224).
ENTER_BASE_PAGE = 0
FIRST_PAGE = 1
# MOVE HIGH-VALUES TO TRAN-ID (line 260), the F8 key when no last key is kept. No record
# follows it, so a response that carries it as last_key makes F8 answer NEXT-PAGE-NO.
NO_NEXT_PAGE_KEY = HIGH_VALUES_TRAN_ID
# WS-TIMESTAMP-DT-YYYY(3:2), -DT-MM and -DT-DD of the TRAN-ORIG-TS text (lines 384-388).
YEAR_DIGITS_SLICE = slice(2, 4)
MONTH_SLICE = slice(5, 7)
DAY_SLICE = slice(8, 10)


@dataclass
class ListState:
    """One request's CT00 working storage: CDEMO-CT00-INFO, WS-MESSAGE and the map rows.

    ``firstKey`` and ``lastKey`` are CDEMO-CT00-TRNID-FIRST and -TRNID-LAST, ``page`` is
    CDEMO-CT00-PAGE-NUM, and ``message`` is WS-MESSAGE, which each later assignment overwrites
    as the program's MOVEs do. ``rows`` are the TRANSACT records shown, in ascending key order.
    ``searchCleared`` records MOVE SPACE TO TRNIDINO (lines 228, 325): the map's search field
    is sent blank.
    """

    firstKey: str
    lastKey: str
    page: int
    message: str = NO_MESSAGE
    rows: list[dict[str, Any]] = field(default_factory=list)
    searchCleared: bool = False


async def ListTransactions(
    context: OnlineContext, query: TransactionListQuery
) -> TransactionListResponse:
    """Port the COTRN00 browse keys: ENTER and the first entry, F8 and F7 (MAIN-PARA 107-136).

    The first entry (lines 112-116) and ENTER run PROCESS-ENTER-KEY with no selection, browsing
    from ``search_tran_id``. F8 is PROCESS-PF8-KEY (lines 257-274) and F7 PROCESS-PF7-KEY
    (lines 234-252), followed by PROCESS-PAGE-FORWARD or PROCESS-PAGE-BACKWARD.

    Args:
        context: The regular-user request context; its session serves the TRANSACT browse.
        query: Forward with an empty ``start_key`` is ENTER from ``search_tran_id``; forward
            with a ``start_key`` is F8 after that last key shown; backward is F7 before that
            first key shown. ``page`` is the page shown, and ``first_key`` its first key, which
            F8 and ENTER need to return the shown rows when the program leaves them in place.

    Returns:
        The page of at most 10 transactions, its page number and keys, and the ERRMSG literal
        the key leaves (any of the five paging literals or ``Tran ID must be Numeric ...``),
        or an empty message.

    Raises:
        LegacyMessageError: 503 ``Unable to lookup transaction...`` while the batch window
            holds TRANSACT or on a database failure.
    """
    repository = TransactionRepository(context)
    page = _NormalisedPage(query.page)
    # The schema's direction defaults to forward, so an empty start key, not a missing
    # direction, marks ENTER; a start key holding only spaces is F8 with a blank last key.
    if query.direction == Direction.BACKWARD:
        state = ListState(firstKey=query.start_key, lastKey="", page=page)
        await _ProcessPf7Key(repository, state)
    elif query.start_key == "":
        state = ListState(firstKey=query.first_key, lastKey="", page=page)
        await _ProcessEnterKey(repository, state, query.search_tran_id)
    else:
        state = ListState(firstKey=query.first_key, lastKey=query.start_key, page=page)
        await _ProcessPf8Key(repository, state)
    return _BuildListResponse(state, TransactionListResponse)


async def ProcessTransactionSelection(
    context: OnlineContext, request: TransactionSelectionRequest
) -> TransactionSelectionResponse:
    """Port ENTER with selection codes, PROCESS-ENTER-KEY (lines 146-229).

    The first row with a non-blank code is the selection (EVALUATE TRUE, lines 148-182). With a
    non-blank TRAN-ID, ``S`` or ``s`` routes to the Transaction View, ending the request as the
    XCTL to COTRN01C ended the task. Any other code sets ``Invalid selection. Valid value is S``
    and processing continues: the list is browsed from ``search_tran_id``, and a literal that
    browse sets replaces it.

    Args:
        context: The regular-user request context; its session serves the TRANSACT browse.
        request: The shown rows' codes in screen order, the search key as keyed, and the first
            key and page number of the page shown.

    Returns:
        The route of a valid selection with empty list fields, or ``route`` None with the
        re-browsed page, or the unchanged page, and its message.

    Raises:
        LegacyMessageError: 503 ``Unable to lookup transaction...`` while the batch window
            holds TRANSACT or on a database failure.
    """
    selection = _FindFirstSelection(request.selections)
    state = ListState(firstKey=request.first_key, lastKey="", page=_NormalisedPage(request.page))
    if selection is not None and not _IsBlank(selection.tran_id):
        if selection.selection in SELECT_CODES:
            return TransactionSelectionResponse(route=_ViewRoute(selection.tran_id))
        state.message = INVALID_SELECTION_MESSAGE
    repository = TransactionRepository(context)
    await _ProcessEnterKey(repository, state, request.search_tran_id)
    return _BuildListResponse(state, TransactionSelectionResponse)


def _IsBlank(text: str) -> bool:
    """Return whether screen text is SPACES OR LOW-VALUES: empty or only spaces."""
    return text.strip(BLANK_CHARACTER) == ""


def _NormalisedPage(page: int) -> int:
    """Return a page number as CDEMO-CT00-PAGE-NUM PIC 9(08) holds it: its low eight digits."""
    return page % PAGE_NUMBER_MODULUS


def _FindFirstSelection(
    selections: Sequence[TransactionSelection],
) -> TransactionSelection | None:
    """Return the first row whose SELnnnn code is not blank; later codes are ignored."""
    for selection in selections:
        if not _IsBlank(selection.selection):
            return selection
    return None


def _ViewRoute(tranId: str) -> str:
    """Return the Transaction View route with the selected TRAN-ID exactly as displayed."""
    return f"{VIEW_ROUTE}?{urlencode({ROUTE_TRAN_ID_PARAMETER: tranId})}"


def _IsNumericKey(searchTranId: str) -> bool:
    """Return whether TRNIDINI IS NUMERIC: all 16 positions of the field are digits 0-9.

    BMS pads a shorter entry with spaces, so ``123`` is not numeric. Only ASCII digits count,
    as COBOL's class test accepts no other digit characters.
    """
    paddedKey = searchTranId.ljust(SEARCH_KEY_LENGTH, BLANK_CHARACTER)
    return NUMERIC_CHARACTERS.issuperset(paddedKey)


async def _Lookup(
    browse: Awaitable[list[dict[str, Any]] | None], operation: str
) -> list[dict[str, Any]] | None:
    """Await one TRANSACT browse; a file condition becomes ``Unable to lookup transaction...``.

    Ports the WHEN OTHER branches of STARTBR-, READNEXT- and READPREV-TRANSACT-FILE (lines
    612-618, 646-652, 680-686), their DISPLAY of the condition codes included, logged with
    ``operation``, the CICS commands of the browse awaited. FileNotOpenError, the NOTOPEN of a
    TRANSACT the batch window closed, belongs to the RepositoryError family.
    """
    try:
        return await browse
    except RepositoryError as exc:
        LogFileCondition(LOGGER, TRANSACTION_ID, operation, exc)
        raise LegacyMessageError(
            TRAN_LOOKUP_FAILED_MESSAGE, field=SEARCH_CURSOR_FIELD, httpStatus=LOOKUP_ERROR_STATUS
        ) from exc


async def _ProcessEnterKey(
    repository: TransactionRepository, state: ListState, searchTranId: str
) -> None:
    """Apply the TRNIDIN search key, then browse forward from it (lines 206-229).

    A blank key is LOW-VALUES, the lowest key (line 207). A non-numeric key sets ERR-FLG, so
    the list is not rebuilt. Otherwise the page number goes to 0 and PROCESS-PAGE-FORWARD reads
    from the first TRAN-ID at or after the key, as ENTER skips no READNEXT (line 285).
    """
    if not _IsBlank(searchTranId) and not _IsNumericKey(searchTranId):
        await _ProcessNonNumericSearch(repository, state)
        return
    state.page = ENTER_BASE_PAGE
    startKey = None if _IsBlank(searchTranId) else searchTranId
    browse = repository.BrowseForward(startKey, inclusive=True, limit=PAGE_SIZE)
    records = await _Lookup(browse, BROWSE_FORWARD_OPERATION)
    if records is None:
        await _ProcessStartNotFound(repository, state)
        return
    _ApplyForwardRows(state, records)


async def _ProcessNonNumericSearch(repository: TransactionRepository, state: ListState) -> None:
    """A non-numeric search key: ``Tran ID must be Numeric ...`` over the unchanged rows.

    The program still moves 0 to CDEMO-CT00-PAGE-NUM (line 224) and issues the STARTBR of
    PROCESS-PAGE-FORWARD (line 281) from TRAN-ID as no MOVE has set it (LOW-VALUES) before
    ERR-FLG skips the rest. The map keeps the page number it showed, but the response carries
    the 0 the COMMAREA holds, so the next F7 answers ``You are already at the top of the
    page...`` as it does in the program. On an empty TRANSACT that STARTBR is NOTFND, whose
    ``You are at the top of the page...`` replaces the message (lines 605-611).
    """
    state.message = TRAN_ID_NOT_NUMERIC_MESSAGE
    state.page = ENTER_BASE_PAGE
    await _RereadShownPage(repository, state)
    if not state.rows and await _TransactFileIsEmpty(repository):
        state.message = AT_TOP_OF_PAGE_MESSAGE


async def _TransactFileIsEmpty(repository: TransactionRepository) -> bool:
    """Return whether a STARTBR at LOW-VALUES is NOTFND: TRANSACT holds no record."""
    browse = repository.BrowseForward(None, inclusive=True, limit=0)
    return await _Lookup(browse, STARTBR_OPERATION) is None


async def _ProcessStartNotFound(repository: TransactionRepository, state: ListState) -> None:
    """ENTER's STARTBR NOTFND: no TRAN-ID at or after the search key (lines 605-611, 314-326).

    It sets TRANSACT-EOF but not ERR-FLG, so PROCESS-PAGE-FORWARD reads no row, sets
    NEXT-PAGE-NO and shows page 0 over the unchanged rows with TRNIDIN blanked (line 325).
    last_key becomes HIGH-VALUES, after which no record follows, so the next F8 answers ``You
    are already at the bottom of the page...``.
    """
    state.message = AT_TOP_OF_PAGE_MESSAGE
    await _RereadShownPage(repository, state)
    state.lastKey = NO_NEXT_PAGE_KEY
    state.searchCleared = True


async def _ProcessPf8Key(repository: TransactionRepository, state: ListState) -> None:
    """F8: the page after the last key shown (PROCESS-PF8-KEY, PROCESS-PAGE-FORWARD).

    A blank last key is HIGH-VALUES (line 260). The READNEXT issued right after STARTBR (line
    286) discards the first record at or after the last key. With no record after that one,
    the program holds NEXT-PAGE-NO and answers ``You are already at the bottom of the page...``
    over the unchanged rows (lines 267-274).
    """
    startKey = NO_NEXT_PAGE_KEY if _IsBlank(state.lastKey) else state.lastKey
    browse = repository.BrowseForward(startKey, inclusive=True, limit=PAGE_SIZE + 1)
    records = await _Lookup(browse, BROWSE_FORWARD_OPERATION)
    if records is None or len(records) <= 1:
        state.message = ALREADY_AT_BOTTOM_MESSAGE
        await _RereadBottomPage(repository, state, records)
        return
    _ApplyForwardRows(state, records[1:])


async def _ProcessPf7Key(repository: TransactionRepository, state: ListState) -> None:
    """F7: the page before the first key shown (PROCESS-PF7-KEY, PROCESS-PAGE-BACKWARD).

    NEXT-PAGE-YES is set before the page test (line 242) and needs no state here: a record
    follows every page F7 shows, so the next F8 pages on. Page 1 or 0 answers ``You are already
    at the top of the page...`` over the unchanged rows (lines 245-251). A blank first key is
    LOW-VALUES (line 237). STARTBR NOTFND leaves the rows in place (lines 605-611).
    """
    if state.page <= FIRST_PAGE:
        state.message = ALREADY_AT_TOP_MESSAGE
        await _RereadShownPage(repository, state)
        return
    startKey = None if _IsBlank(state.firstKey) else state.firstKey
    browse = repository.BrowseBackward(startKey, limit=PAGE_SIZE)
    records = await _Lookup(browse, BROWSE_BACKWARD_OPERATION)
    if records is None:
        state.message = AT_TOP_OF_PAGE_MESSAGE
        await _RereadShownPage(repository, state)
        return
    _ApplyBackwardRows(state, records)


def _ApplyForwardRows(state: ListState, records: Sequence[dict[str, Any]]) -> None:
    """Fill the page from PROCESS-PAGE-FORWARD's READNEXTs (lines 289-320).

    ``records`` holds up to 10 page rows and the look-ahead READNEXT. Without the look-ahead
    the browse reached ENDFILE, whose ``You have reached the bottom of the page...`` replaces
    any earlier message (lines 639-645), and no record follows the last row shown, which is
    NEXT-PAGE-NO. The page number grows by 1 once a row was read (lines 305-319), and TRNIDIN
    is sent blank (line 325).
    """
    pageRows = list(records[:PAGE_SIZE])
    _ShowRows(state, pageRows)
    if pageRows:
        state.page = (state.page + 1) % PAGE_NUMBER_MODULUS
    if len(records) <= PAGE_SIZE:
        state.message = REACHED_BOTTOM_MESSAGE
    state.searchCleared = True


def _ApplyBackwardRows(state: ListState, records: Sequence[dict[str, Any]]) -> None:
    """Fill the page from PROCESS-PAGE-BACKWARD's READPREVs (lines 343-369).

    ``records`` holds the records before the first key, nearest first: up to 10 page rows,
    which the program places in rows 10 down to 1, and the look-ahead READPREV. Fewer than 10
    is ENDFILE: ``You have reached the top of the page...``, the page number unchanged, and
    CDEMO-CT00-TRNID-FIRST kept, as row 1 stays empty. Exactly 10 with ENDFILE on the
    look-ahead resets the page to 1; a look-ahead record lowers it by 1.
    """
    browseKey = state.firstKey
    _ShowRows(state, list(reversed(records[:PAGE_SIZE])))
    if len(state.rows) < PAGE_SIZE:
        state.message = REACHED_TOP_MESSAGE
        state.firstKey = browseKey
        state.lastKey = state.lastKey or browseKey
    elif len(records) > PAGE_SIZE:
        state.page = state.page - 1 if state.page > FIRST_PAGE else FIRST_PAGE
    else:
        state.message = REACHED_TOP_MESSAGE
        state.page = FIRST_PAGE


async def _RereadShownPage(repository: TransactionRepository, state: ListState) -> None:
    """Read again the page a re-sent map leaves shown: 10 TRAN-IDs from the first key shown.

    Every outcome replaces the client's rows, so the rows the program leaves on the map are
    read back. A blank first key means no page is shown.
    """
    records: list[dict[str, Any]] | None = []
    if not _IsBlank(state.firstKey):
        browse = repository.BrowseForward(state.firstKey, inclusive=True, limit=PAGE_SIZE - 1)
        records = await _Lookup(browse, BROWSE_FORWARD_OPERATION)
    _ShowRows(state, records or [])


async def _RereadBottomPage(
    repository: TransactionRepository, state: ListState, records: list[dict[str, Any]] | None
) -> None:
    """Read again the page an F8 refusal leaves shown, given the F8 browse's ``records``.

    With a first key the page is read from it. A client that sends only the last key gets the
    10 records ending at the first record at or after it, which is exact when that page is
    full; with no such record nothing is shown.
    """
    if not _IsBlank(state.firstKey) or not records:
        await _RereadShownPage(repository, state)
        return
    lastRecord = records[0]
    browse = repository.BrowseBackward(lastRecord["tran_id"], limit=PAGE_SIZE - 2)
    earlierRecords = await _Lookup(browse, BROWSE_BACKWARD_OPERATION) or []
    _ShowRows(state, [*reversed(earlierRecords), lastRecord])


def _ShowRows(state: ListState, records: list[dict[str, Any]]) -> None:
    """Put records on the map with TRNID-FIRST and -LAST at the first and last row shown.

    POPULATE-TRAN-DATA moves row 1's and row 10's TRAN-ID (lines 392-393, 438-439). On a
    shorter page the last row stands for row 10, so F8 finds no record after it.
    """
    state.rows = records
    state.firstKey = records[0]["tran_id"] if records else ""
    state.lastKey = records[-1]["tran_id"] if records else ""


def _BuildRow(record: Mapping[str, Any]) -> TransactionListRow:
    """Return TRNIDnn, TDATEnn, TDESCnn and TAMTnnn of one TRAN-RECORD (lines 383-442)."""
    return TransactionListRow(
        tran_id=record["tran_id"],
        tran_date=_TransactionDate(record),
        tran_desc=record["tran_desc"][:ROW_DESCRIPTION_LENGTH],
        tran_amt=StoreInto(record["tran_amt"], AMOUNT_INTEGER_DIGITS, AMOUNT_DECIMAL_DIGITS),
    )


def _TransactionDate(record: Mapping[str, Any]) -> str:
    """Return TDATEnn, MM/DD/YY, from the first 10 characters of the TRAN-ORIG-TS text.

    The stored timestamp is rebuilt in its legacy text form, which begins YYYY-MM-DD in every
    non-blank form; a blank TRAN-ORIG-TS gives ``  /  /  ``.
    """
    origTs = EncodeTimestamp(record["orig_ts"], record["orig_ts_format"])
    return f"{origTs[MONTH_SLICE]}/{origTs[DAY_SLICE]}/{origTs[YEAR_DIGITS_SLICE]}"


def _BuildListResponse[ResponseModel: TransactionListResponse](
    state: ListState, responseModel: type[ResponseModel]
) -> ResponseModel:
    """Return the COTRN00 screen: rows, page number, keys, ERRMSG, cursor and TRNIDIN blanking."""
    return responseModel(
        rows=[_BuildRow(record) for record in state.rows],
        page=state.page,
        first_key=state.firstKey,
        last_key=state.lastKey,
        message=state.message,
        cursor_field=SEARCH_CURSOR_FIELD,
        search_cleared=state.searchCleared,
    )
