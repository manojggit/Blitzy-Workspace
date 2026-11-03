"""Replaces: COCRDLIC (CCLI), BMS COCRDLI, copybooks CVCRD01Y, CVACT02Y, CSUSR01Y

Serves ``GET /api/v1/cards`` (ENTER, F7, F8) and ``POST /api/v1/cards/selection`` (ENTER with
selection codes): seven cards a page in EBCDIC card-number order, filtered in all-field mode.

Paging contract, replacing WS-THIS-PROGCOMMAREA: every list response carries ``page`` and the
opaque ``first_key`` and ``last_key``, which both hold the COMMAREA paging state (first and last
card keys, CA-NEXT-PAGE-IND, CA-LAST-PAGE-DISPLAYED). ENTER sends ``startKey=first_key`` with no
``direction``, F7 ``first_key`` with ``backward``, F8 ``last_key`` with ``forward``; each echoes
``page`` and the rows' CRDSEL1-7 codes as ``selections``. A missing, expired or foreign key is a
fresh entry on page 1. 2250-EDIT-ARRAY edits the codes after the filters and before any paging:
more than one S or U, or an invalid code, redisplays the list from the lowest key as a 400
carrying the rows, both page keys and ``page``. A single S or U on ``GET /cards`` never
transfers; only ``POST /cards/selection`` returns the view or update action.

Pinned: the program header promises admins all cards, but the code only filters, so every user
lists alike (ported unchanged). The Style B reset of the COMMAREA user type to U (line 320) is
superseded by the session identity. CARDDAT is no batch-window file, so CCLI lists during batch.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken

from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import DecodeCardRef, EncodeCardRef, MaskCardNumber
from app.repositories.card_repository import (
    BROWSE_BACKWARD,
    BROWSE_FORWARD,
    CARD_FILE_NAME,
    CardFilters,
    CardRepository,
)
from app.schemas.cards import (
    CardListQuery,
    CardListResponse,
    CardListRow,
    CardSelectionRequest,
    CardSelectionResponse,
    CardSelectionRow,
)
from app.schemas.common import Direction
from app.services.cards.cocrdslc import (
    ACCOUNT_FILTER_MESSAGE,
    ACCT_ID_FIELD,
    ACCT_ID_LENGTH,
    CARD_FILTER_MESSAGE,
    CARD_NUM_FIELD,
    CARD_NUM_LENGTH,
    READ_OPERATION,
    BuildFileErrorMessage,
    IsAsciiDigits,
    NotFoundError,
    PadScreenText,
)

if TYPE_CHECKING:
    # Type-only: the Settings that OnlineContext.settings carries and the token codecs expect.
    from app.core.config import Settings

LOGGER = logging.getLogger(__name__)

# LIT-THISTRANID (lines 181-182).
TRANSACTION_ID = "CCLI"
# The CICS commands of the 9000-READ-FORWARD and 9100-READ-BACKWARDS browses, which one
# repository call runs, as their logged file conditions name them.
BROWSE_OPERATIONS = {
    BROWSE_FORWARD: "STARTBR/READNEXT",
    BROWSE_BACKWARD: "STARTBR/READPREV",
}

# WS-MAX-SCREEN-LINES (lines 177-178).
PAGE_SIZE = 7
# WS-ERROR-MSG 88 levels and literal MOVEs on reachable paths (lines 121-126, 903, 908, 1219).
NO_RECORDS_FOUND_MESSAGE = "NO RECORDS FOUND FOR THIS SEARCH CONDITION."
NO_PREVIOUS_PAGES_MESSAGE = "NO PREVIOUS PAGES TO DISPLAY"
NO_MORE_PAGES_MESSAGE = "NO MORE PAGES TO DISPLAY"
NO_MORE_RECORDS_MESSAGE = "NO MORE RECORDS TO SHOW"
MORE_THAN_ONE_ACTION_MESSAGE = "PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE"
INVALID_ACTION_CODE_MESSAGE = "INVALID ACTION CODE"
# 88 WS-INFORM-REC-ACTIONS (lines 115-116), set by 1400-SETUP-MESSAGE (lines 912, 919).
RECORD_ACTIONS_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD"
NO_MESSAGE = ""

# 88 SELECT-OK VALUES 'S', 'U' (upper case only) and the program each one transfers to.
VIEW_CODE = "S"
UPDATE_CODE = "U"
SELECTION_ACTIONS = {VIEW_CODE: "view", UPDATE_CODE: "update"}
# 88 SELECT-BLANK VALUES ' ', LOW-VALUES: a blank or unsent CRDSELn field.
BLANK_SELECTION_CODES = ("", " ")
# The CardSelectionRequest list and row attribute that field_errors names, e.g. rows[2].selection.
ROWS_FIELD = "rows"
SELECTION_FIELD = "selection"

# WS-CA-NEXT-PAGE-IND and WS-CA-LAST-PAGE-DISPLAYED 88 values (lines 239-244), as page-key flags.
NEXT_PAGE_YES = "Y"
NEXT_PAGE_NO = "N"
LAST_PAGE_SHOWN = "0"
LAST_PAGE_NOT_SHOWN = "9"
# Page-key payload: WS-CA-FIRST-CARD-NUM X(16), WS-CA-LAST-CARD-NUM X(16), then the two flags.
PAGE_KEY_LENGTH = 2 * CARD_NUM_LENGTH + 2

# WS-CA-SCREEN-NUM PIC 9(1): ADD and SUBTRACT keep only the units digit; 88 CA-FIRST-PAGE VALUE 1.
SCREEN_NUM_MODULUS = 10
FIRST_PAGE = 1
# READPREV at the start of CARDDAT: CICS ENDFILE, which 9100-READ-BACKWARDS takes as WHEN OTHER.
ENDFILE_RESP = 20
ENDFILE_RESP2 = 90
READPREV_ENDFILE_MESSAGE = BuildFileErrorMessage(
    READ_OPERATION, CARD_FILE_NAME, ENDFILE_RESP, ENDFILE_RESP2
)

# CCARD-AID values of the keys COCRDLIC acts on (copybook CVCRD01Y); any other key is ENTER.
KEY_ENTER = "ENTER"
KEY_PAGE_UP = "PFK07"
KEY_PAGE_DOWN = "PFK08"
KEY_BY_DIRECTION = {
    None: KEY_ENTER,
    Direction.FORWARD: KEY_PAGE_DOWN,
    Direction.BACKWARD: KEY_PAGE_UP,
}


@dataclass(frozen=True)
class PageCursor:
    """The WS-THIS-PROGCOMMAREA paging state that a page key carries between requests.

    ``firstCardKey`` and ``lastCardKey`` are WS-CA-FIRST-CARD-NUM and WS-CA-LAST-CARD-NUM ("" when
    no card has been read). ``lastCardKey`` is the look-ahead card that starts the next page.
    """

    firstCardKey: str = ""
    lastCardKey: str = ""
    nextPageExists: bool = False
    lastPageShown: bool = False


# A first entry: INITIALIZE WS-THIS-PROGCOMMAREA and SET CA-LAST-PAGE-NOT-SHOWN (lines 315-325).
FRESH_CURSOR = PageCursor()


@dataclass(frozen=True)
class FilterRule:
    """The field name, BMS width and literal of one COCRDLIC filter edit (2210 or 2220)."""

    fieldName: str
    length: int
    message: str


FILTER_RULES = (
    FilterRule(ACCT_ID_FIELD, ACCT_ID_LENGTH, ACCOUNT_FILTER_MESSAGE),
    FilterRule(CARD_NUM_FIELD, CARD_NUM_LENGTH, CARD_FILTER_MESSAGE),
)


@dataclass(frozen=True)
class FilterEdit:
    """The outcome of 2210-EDIT-ACCOUNT and 2220-EDIT-CARD.

    ``acctId`` and ``cardNum`` are the filters marked ISVALID, or None when blank or invalid.
    ``message`` is the first failing filter's literal and ``fieldErrors`` lists every failing one.
    """

    acctId: str | None
    cardNum: str | None
    message: str
    fieldErrors: tuple[str, ...]

    def Filters(self) -> CardFilters:
        """Return the filters that 9500-FILTER-RECORDS applies to every browsed record."""
        return CardFilters(acctId=self.acctId, cardNum=self.cardNum)


@dataclass(frozen=True)
class ListState:
    """The screen one COCRDLIC pass builds: the paging state, page number, messages and rows."""

    cursor: PageCursor
    pageNumber: int
    message: str = NO_MESSAGE
    infoMessage: str = NO_MESSAGE
    rows: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ListRequest:
    """The mapped key (CCARD-AID) and the COMMAREA state a list request starts from."""

    keyPressed: str
    state: ListState


# POST /cards/selection carries no COMMAREA paging state and no page number (WS-CA-SCREEN-NUM),
# so its selection-error redisplay reads as a first entry: ENTER on page 1, where the page-1 test
# of NO RECORDS FOUND (line 1241) holds.
SELECTION_ENTRY_REQUEST = ListRequest(
    KEY_ENTER, ListState(cursor=FRESH_CURSOR, pageNumber=FIRST_PAGE)
)


@dataclass(frozen=True)
class SelectionEdit:
    """The outcome of 2250-EDIT-ARRAY over the rows' selection codes, in screen order.

    ``selectedIndex`` is I-SELECTED as a 0-based row index (the last S or U row), or None.
    ``flaggedRows`` are the rows whose WS-ROW-CRDSELECT-ERROR is set.
    """

    selectedIndex: int | None
    message: str
    flaggedRows: tuple[int, ...]


def EncodePageKey(cursor: PageCursor, settings: "Settings") -> str:
    """Return the opaque page key holding ``cursor``, replacing the COMMAREA paging fields.

    The payload is both card keys padded to X(16) and the two flags, encrypted as a ``card_ref``
    token, so card numbers leave the server only inside the token.
    """
    payload = (
        cursor.firstCardKey.ljust(CARD_NUM_LENGTH)
        + cursor.lastCardKey.ljust(CARD_NUM_LENGTH)
        + (NEXT_PAGE_YES if cursor.nextPageExists else NEXT_PAGE_NO)
        + (LAST_PAGE_SHOWN if cursor.lastPageShown else LAST_PAGE_NOT_SHOWN)
    )
    return EncodeCardRef(payload, settings)


def DecodePageKey(token: str | None, settings: "Settings") -> PageCursor | None:
    """Return the paging state of a page key, or None for a fresh entry (EIBCALEN = 0).

    A missing, invalid or expired token, or a token whose payload is not a page key (such as a
    row ``card_ref``), is None: the client has lost its COMMAREA.
    """
    if not token:
        return None
    try:
        payload = DecodeCardRef(token, settings)
    except InvalidToken:
        return None
    return CursorOfPayload(payload)


def CursorOfPayload(payload: str) -> PageCursor | None:
    """Return the paging state a decoded page-key payload holds, or None when it holds none."""
    nextFlag = payload[-2:-1]
    shownFlag = payload[-1:]
    if (
        len(payload) != PAGE_KEY_LENGTH
        or nextFlag not in (NEXT_PAGE_YES, NEXT_PAGE_NO)
        or shownFlag not in (LAST_PAGE_SHOWN, LAST_PAGE_NOT_SHOWN)
    ):
        return None
    return PageCursor(
        firstCardKey=payload[:CARD_NUM_LENGTH].rstrip(" "),
        lastCardKey=payload[CARD_NUM_LENGTH : 2 * CARD_NUM_LENGTH].rstrip(" "),
        nextPageExists=nextFlag == NEXT_PAGE_YES,
        lastPageShown=shownFlag == LAST_PAGE_SHOWN,
    )


def EditFilter(filterText: str, rule: FilterRule) -> tuple[str | None, bool]:
    """The shared body of 2210-EDIT-ACCOUNT and 2220-EDIT-CARD; returns (filter, failed).

    Not entered, all spaces or all zeros is FLG-...-BLANK: no filter and no error. Anything but
    ``rule.length`` ASCII digits on the space-padded field (``IS NOT NUMERIC``) fails. COCRDLIC
    has no ``*`` handling, so ``*`` fails as non-numeric.
    """
    paddedText = PadScreenText(filterText, rule.length)
    if not paddedText.strip(" ") or paddedText == "0" * rule.length:
        return None, False
    if len(paddedText) != rule.length or not IsAsciiDigits(paddedText):
        return None, True
    return paddedText, False


def EditFilters(acctText: str, cardText: str) -> FilterEdit:
    """Port of 2210-EDIT-ACCOUNT and 2220-EDIT-CARD (lines 1003-1071) in all-field mode.

    Both edits run, account first. The account literal is moved unconditionally but first, and
    the card literal only while WS-ERROR-MSG-OFF holds, so the first failing literal is kept.
    """
    results = [
        (rule, *EditFilter(filterText, rule))
        for rule, filterText in zip(FILTER_RULES, (acctText, cardText), strict=True)
    ]
    failedRules = [rule for rule, _, failed in results if failed]
    (_, acctId, _), (_, cardNum, _) = results
    return FilterEdit(
        acctId=acctId,
        cardNum=cardNum,
        message=failedRules[0].message if failedRules else NO_MESSAGE,
        fieldErrors=tuple(rule.fieldName for rule in failedRules),
    )


def RaiseFilterError(edit: FilterEdit) -> None:
    """Raise the 400 of a failed filter edit (INPUT-ERROR with FLG-...-NOT-OK).

    No browse runs (lines 431-435) and the selection fields are protected, so the client keeps
    the rows it shows. 1400-SETUP-MESSAGE sets no information line (line 898).
    """
    if not edit.message:
        return
    raise LegacyMessageError(
        edit.message,
        field=edit.fieldErrors[0],
        httpStatus=HTTP_EDIT_FAILURE,
        details=LegacyErrorDetails(infoMessage=NO_MESSAGE, fieldErrors=edit.fieldErrors),
    )


def ResolveListRequest(query: CardListQuery, settings: "Settings") -> ListRequest:
    """Port of the COMMAREA retrieval, PF-key mapping and last-page reset (lines 315-414).

    No direction is ENTER. A fresh entry is page 1 (SET CA-FIRST-PAGE). Otherwise the page is
    the echoed ``page`` as PIC 9(1) holds it: FastAPI fills the default 0 for an omitted page, so
    absence cannot be told from page 0, which the read turns into 1 once a row is read.
    """
    keyPressed = KEY_BY_DIRECTION.get(query.direction, KEY_ENTER)
    cursor = DecodePageKey(query.start_key, settings)
    if cursor is None:
        return ListRequest(keyPressed, ListState(cursor=FRESH_CURSOR, pageNumber=FIRST_PAGE))
    # IF CCARD-AID-PFK08 CONTINUE ELSE SET CA-LAST-PAGE-NOT-SHOWN (lines 410-414).
    keptShown = cursor.lastPageShown and keyPressed == KEY_PAGE_DOWN
    state = ListState(
        cursor=replace(cursor, lastPageShown=keptShown),
        pageNumber=query.page % SCREEN_NUM_MODULUS,
    )
    return ListRequest(keyPressed, state)


async def ReadListPage(
    context: OnlineContext, filters: CardFilters, request: ListRequest
) -> ListState:
    """Port of the 0000-MAIN EVALUATE for ENTER, F7 and F8 (lines 439-513, 572-582).

    F7 on page 1 re-reads forward from the first card. F8 with a next page adds 1 to the page
    (9 wraps to 0) and reads from the look-ahead card. F7 elsewhere subtracts 1 (unsigned) and
    reads backward. Anything else, F8 without a next page included, re-reads the page shown.
    """
    state = request.state
    cursor = state.cursor
    if request.keyPressed == KEY_PAGE_UP and state.pageNumber == FIRST_PAGE:
        return await ReadForward(context, filters, state, cursor.firstCardKey)
    if request.keyPressed == KEY_PAGE_DOWN and cursor.nextPageExists:
        nextPage = (state.pageNumber + 1) % SCREEN_NUM_MODULUS
        nextState = replace(state, pageNumber=nextPage)
        return await ReadForward(context, filters, nextState, cursor.lastCardKey)
    if request.keyPressed == KEY_PAGE_UP:
        previousPage = abs(state.pageNumber - 1) % SCREEN_NUM_MODULUS
        previousState = replace(state, pageNumber=previousPage)
        return await ReadBackward(context, filters, previousState)
    return await ReadForward(context, filters, state, cursor.firstCardKey)


def FileErrorOf(error: RepositoryError) -> LegacyMessageError:
    """Port of the READNEXT and READPREV WHEN OTHER branches: the READ file-error literal.

    The literal names LIT-CARD-FILE with the condition's RESP and RESP2 (lines 1222-1230,
    1246-1254, 1308-1317, 1361-1369); the cursor stays on the account filter.
    """
    message = BuildFileErrorMessage(READ_OPERATION, CARD_FILE_NAME, error.resp, error.resp2)
    return LegacyMessageError(message, field=ACCT_ID_FIELD, httpStatus=HTTP_FILE_ERROR)


async def BrowseOrRaise(
    context: OnlineContext, filters: CardFilters, startCard: str, direction: str
) -> list[dict[str, Any]]:
    """STARTBR GTEQ at ``startCard`` and the filtered READNEXT or READPREV loop of one page.

    An empty start card is the initialised RID, which sorts before every card number.
    """
    try:
        return await CardRepository(context).BrowseCards(
            filters, startCard or None, direction, PAGE_SIZE
        )
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, BROWSE_OPERATIONS[direction], error)
        raise FileErrorOf(error) from error


async def ReadForward(
    context: OnlineContext, filters: CardFilters, state: ListState, startCard: str
) -> ListState:
    """Port of 9000-READ-FORWARD (lines 1123-1260).

    Up to 7 filtered rows from ``startCard`` inclusive. The extra READNEXT after the 7th row
    skips 9500-FILTER-RECORDS, so the next page starts at that look-ahead card, matching or not.
    A row read on page 0 makes it page 1 (lines 1177-1181).
    """
    records = await BrowseOrRaise(context, filters, startCard, BROWSE_FORWARD)
    pageRows = tuple(records[:PAGE_SIZE])
    lookAhead = records[PAGE_SIZE] if len(records) > PAGE_SIZE else None
    pageNumber = FIRST_PAGE if pageRows and state.pageNumber == 0 else state.pageNumber
    return replace(
        state,
        cursor=ForwardCursor(state.cursor, pageRows, lookAhead),
        pageNumber=pageNumber,
        message=ForwardMessage(state, pageRows, lookAhead),
        rows=pageRows,
    )


def ForwardCursor(
    previous: PageCursor, pageRows: Sequence[Mapping[str, Any]], lookAhead: Mapping[str, Any] | None
) -> PageCursor:
    """Return the COMMAREA keys and next-page flag after 9000-READ-FORWARD.

    With no row read the first card key is not overwritten. The last card key matters only
    while a next page exists, when it is the look-ahead card.
    """
    if not pageRows:
        return replace(previous, nextPageExists=False)
    lastRecord = lookAhead if lookAhead is not None else pageRows[-1]
    return replace(
        previous,
        firstCardKey=pageRows[0]["card_num"],
        lastCardKey=lastRecord["card_num"],
        nextPageExists=lookAhead is not None,
    )


def ForwardMessage(
    state: ListState, pageRows: Sequence[Mapping[str, Any]], lookAhead: Mapping[str, Any] | None
) -> str:
    """Return WS-ERROR-MSG after 9000-READ-FORWARD.

    ENDFILE sets ``NO MORE RECORDS TO SHOW`` while no message is set, the look-ahead ENDFILE
    after a full page included. On page 1 with no row read, the unconditional SET of
    ``NO RECORDS FOUND FOR THIS SEARCH CONDITION.`` overrides it (lines 1241-1245).
    """
    message = state.message
    if lookAhead is None and not message:
        message = NO_MORE_RECORDS_MESSAGE
    if not pageRows and state.pageNumber == FIRST_PAGE:
        message = NO_RECORDS_FOUND_MESSAGE
    return message


async def ReadBackward(context: OnlineContext, filters: CardFilters, state: ListState) -> ListState:
    """Port of 9100-READ-BACKWARDS (lines 1264-1380).

    The last card key becomes the first, and a next page exists. Up to 7 filtered rows before
    the first card are shown in ascending order. When 7 are found the lowest becomes the first
    card; fewer means READPREV hit the start of the file, whose ENDFILE takes WHEN OTHER: the
    file-error literal is moved unconditionally, the rows found are still shown, and the first
    card key is kept.
    """
    cursor = state.cursor
    records = await BrowseOrRaise(context, filters, cursor.firstCardKey, BROWSE_BACKWARD)
    pageRows = tuple(reversed(records))
    reachedStart = len(pageRows) < PAGE_SIZE
    firstCard = cursor.firstCardKey if reachedStart else pageRows[0]["card_num"]
    backwardCursor = replace(
        cursor, firstCardKey=firstCard, lastCardKey=cursor.firstCardKey, nextPageExists=True
    )
    message = READPREV_ENDFILE_MESSAGE if reachedStart else state.message
    return replace(state, cursor=backwardCursor, message=message, rows=pageRows)


def ApplyListMessages(state: ListState, keyPressed: str) -> ListState:
    """Port of 1400-SETUP-MESSAGE (lines 895-930) for valid filters.

    F7 on page 1 gives ``NO PREVIOUS PAGES TO DISPLAY``. F8 with no next page gives
    ``NO MORE PAGES TO DISPLAY`` once the last page was shown, else marks it shown. Every other
    case shows the record-actions line, because 1100-SCREEN-INIT always sets WS-NO-INFO-MESSAGE.
    The information line is never shown with ``NO RECORDS FOUND FOR THIS SEARCH CONDITION.``.
    """
    cursor = state.cursor
    message, infoMessage, lastPageShown = state.message, RECORD_ACTIONS_INFO, cursor.lastPageShown
    if keyPressed == KEY_PAGE_UP and state.pageNumber == FIRST_PAGE:
        message, infoMessage = NO_PREVIOUS_PAGES_MESSAGE, NO_MESSAGE
    elif keyPressed == KEY_PAGE_DOWN and not cursor.nextPageExists:
        if lastPageShown:
            message, infoMessage = NO_MORE_PAGES_MESSAGE, NO_MESSAGE
        lastPageShown = True
    if message == NO_RECORDS_FOUND_MESSAGE:
        infoMessage = NO_MESSAGE
    return replace(
        state,
        cursor=replace(cursor, lastPageShown=lastPageShown),
        message=message,
        infoMessage=infoMessage,
    )


def BuildRows(records: Sequence[Mapping[str, Any]], settings: "Settings") -> list[CardListRow]:
    """Port of 1200-SCREEN-ARRAY-INIT: one row per card shown.

    The card number leaves only masked and inside its ``card_ref``; the CVV and the embossed
    name are never read into a row.
    """
    return [
        CardListRow(
            card_ref=EncodeCardRef(record["card_num"], settings),
            acct_id=record["acct_id"],
            card_num_masked=MaskCardNumber(record["card_num"]),
            active_status=record["active_status"],
        )
        for record in records
    ]


def BuildListResponse(state: ListState, settings: "Settings") -> CardListResponse:
    """Port of 1000-SEND-MAP for a shown page, with the cursor on ACCTSID (lines 884-886)."""
    pageKey = EncodePageKey(state.cursor, settings)
    return CardListResponse(
        rows=BuildRows(state.rows, settings),
        page=state.pageNumber,
        first_key=pageKey,
        last_key=pageKey,
        message=state.message,
        info_message=state.infoMessage,
        field_errors=[],
        cursor_field=ACCT_ID_FIELD,
    )


async def ListCards(context: OnlineContext, query: CardListQuery) -> CardListResponse:
    """Port of the CCLI ENTER, F7 and F8 paths: 2200-EDIT-INPUTS, 0000-MAIN, 1000-SEND-MAP.

    The filters are edited first, then the rows' selection codes (2250-EDIT-ARRAY), because
    WHEN INPUT-ERROR precedes every paging arm (lines 419-438). A selection error raises the
    400 of the redisplay, with ``page``. A single S or U pages like a blank code: the XCTL arms
    test CCARD-AID-ENTER, and this route never transfers. Raises 400 for a filter edit failure
    and 503 for a CARDDAT file condition. Admin and regular users list alike: the header's admin
    all-cards mode does not exist in the code.
    """
    edit = EditFilters(query.acct_id, query.card_num)
    RaiseFilterError(edit)
    request = ResolveListRequest(query, context.settings)
    selection = EditSelections(query.selections)
    if selection.message:
        state = await RedisplayAfterSelectionError(
            context, edit.Filters(), request, selection.message
        )
        raise SelectionError(state, selection, context.settings, withPage=True)
    state = await ReadListPage(context, edit.Filters(), request)
    return BuildListResponse(ApplyListMessages(state, request.keyPressed), context.settings)


def EditSelections(codes: Sequence[str]) -> SelectionEdit:
    """Port of 2250-EDIT-ARRAY (lines 1079-1115) over the selection codes in screen order.

    More than one S or U gives ``PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE`` and flags each
    of them. The last S or U row is selected. Any other non-blank code, lower-case ``s`` and
    ``u`` included, is flagged and gives ``INVALID ACTION CODE`` while no message is set.
    """
    moreThanOne = sum(1 for code in codes if code in SELECTION_ACTIONS) > 1
    message = MORE_THAN_ONE_ACTION_MESSAGE if moreThanOne else NO_MESSAGE
    selectedIndex: int | None = None
    flaggedRows: list[int] = []
    for rowIndex, code in enumerate(codes):
        if code in SELECTION_ACTIONS:
            selectedIndex = rowIndex
            if moreThanOne:
                flaggedRows.append(rowIndex)
        elif code not in BLANK_SELECTION_CODES:
            flaggedRows.append(rowIndex)
            message = message or INVALID_ACTION_CODE_MESSAGE
    return SelectionEdit(selectedIndex, message, tuple(flaggedRows))


def SelectionFieldName(rowIndex: int) -> str:
    """Return the field_errors name of a row's selection code, e.g. ``rows[2].selection``."""
    return f"{ROWS_FIELD}[{rowIndex}].{SELECTION_FIELD}"


def SelectionCursorField(flaggedRows: Sequence[int]) -> str:
    """Return the cursor of a selection error, from 1250-SETUP-ARRAY-ATTRIBS (lines 751-831).

    Rows 2 to 7 get ``MOVE -1`` to their CRDSELnL, so the first of them flagged holds the
    cursor. Row 1 gets none, so with only row 1 flagged the cursor falls to ACCTSID (IC).
    """
    cursorRows = [rowIndex for rowIndex in flaggedRows if rowIndex >= 1]
    return SelectionFieldName(cursorRows[0]) if cursorRows else ACCT_ID_FIELD


async def RedisplayAfterSelectionError(
    context: OnlineContext, filters: CardFilters, request: ListRequest, message: str
) -> ListState:
    """Port of the WHEN INPUT-ERROR redisplay (lines 419-438) after a selection error.

    9000-READ-FORWARD starts from the lowest key, because this path never moves a card key to
    the INITIALIZEd WS-CARD-RID-CARDNUM (ported unchanged), with the page number and paging
    flags of ``request``; a row read on page 0 makes it page 1. The selection literal is already
    set, so ``NO MORE RECORDS TO SHOW`` does not replace it. 1400-SETUP-MESSAGE then runs with
    the key pressed: F7 on page 1 overrides the literal with ``NO PREVIOUS PAGES TO DISPLAY``.
    """
    state = replace(request.state, message=message)
    readState = await ReadForward(context, filters, state, FRESH_CURSOR.firstCardKey)
    return ApplyListMessages(readState, request.keyPressed)


def SelectionError(
    state: ListState, selection: SelectionEdit, settings: "Settings", *, withPage: bool = False
) -> LegacyMessageError:
    """Return the 400 of a selection error, carrying the redisplayed rows and page keys.

    ``withPage`` adds the page number after the redisplay's read, as 1100-SCREEN-INIT shows it
    in PAGENO: ``GET /cards`` echoes a page, while ``POST /cards/selection`` carries none and
    its client keeps the page it shows.
    """
    pageKey = EncodePageKey(state.cursor, settings)
    extraBody = {
        ROWS_FIELD: [row.model_dump(mode="json") for row in BuildRows(state.rows, settings)],
        "first_key": pageKey,
        "last_key": pageKey,
    }
    if withPage:
        extraBody["page"] = state.pageNumber
    return LegacyMessageError(
        state.message,
        field=SelectionCursorField(selection.flaggedRows),
        httpStatus=HTTP_EDIT_FAILURE,
        details=LegacyErrorDetails(
            infoMessage=state.infoMessage,
            fieldErrors=tuple(SelectionFieldName(row) for row in selection.flaggedRows),
            extraBody=extraBody,
        ),
    )


def TransferToCard(row: CardSelectionRow, settings: "Settings") -> CardSelectionResponse:
    """Port of the XCTL to COCRDSLC (S) or COCRDUPC (U) with the row's card (lines 517-569).

    No record is read before the transfer. A ``card_ref`` that is invalid, expired or not a card
    key (such as a page key) gives the 404 ``Did not find cards for this search condition``.
    """
    try:
        cardNum = DecodeCardRef(row.card_ref, settings)
    except InvalidToken:
        raise NotFoundError() from None
    if len(cardNum) > CARD_NUM_LENGTH:
        raise NotFoundError()
    return CardSelectionResponse(
        action=SELECTION_ACTIONS[row.selection],
        card_ref=EncodeCardRef(cardNum, settings),
        message=NO_MESSAGE,
    )


async def ProcessCardSelection(
    context: OnlineContext, request: CardSelectionRequest
) -> CardSelectionResponse:
    """Port of the CCLI ENTER path with selection codes: 2200-EDIT-INPUTS, then 0000-MAIN.

    The filters are edited first and a filter error skips 2250-EDIT-ARRAY (lines 1075-1077).
    A selection error raises 400 with the rows redisplayed as a first entry on page 1, and no
    ``page`` in the body. One S or U returns ``view`` or ``update`` with a fresh ``card_ref``:
    this is the only transfer, as ``GET /cards`` never transfers. No selection returns no
    action: the WHEN OTHER re-read, which the client makes with ``GET /cards`` and
    ``startKey=first_key``.
    """
    edit = EditFilters(request.acct_id, request.card_num)
    RaiseFilterError(edit)
    selection = EditSelections(tuple(row.selection for row in request.rows))
    if selection.message:
        state = await RedisplayAfterSelectionError(
            context, edit.Filters(), SELECTION_ENTRY_REQUEST, selection.message
        )
        raise SelectionError(state, selection, context.settings)
    if selection.selectedIndex is None:
        return CardSelectionResponse(action=None, card_ref="", cursor_field=ACCT_ID_FIELD)
    return TransferToCard(request.rows[selection.selectedIndex], context.settings)
