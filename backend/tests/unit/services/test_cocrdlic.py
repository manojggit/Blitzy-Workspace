"""Unit tests for COCRDLIC (CCLI, Card List, BMS map COCRDLI): app.services.cards.cocrdlic.

ListCards and ProcessCardSelection run directly against the seeded test database, whose
cards.cards rows are the 50 CARDDAT records of copybook CVACT02Y. Expected values are typed from
cbl/COCRDLIC.cbl:

- WS-MAX-SCREEN-LINES (lines 177-178): 7 rows a page, in CARD-NUM key order. That is EBCDIC
    order, computed here from cards.cards with code page 037, never read from the service.
- 2210-EDIT-ACCOUNT (lines 1017-1025) moves ``ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT
    NUMBER`` unguarded and first; 2220-EDIT-CARD (lines 1051-1060) moves ``CARD ID FILTER,IF
    SUPPLIED MUST BE A 16 DIGIT NUMBER`` only while WS-ERROR-MSG-OFF holds. Both fields stay
    flagged (all-field mode), and 1400-SETUP-MESSAGE sets no information line for them.
- 9000-READ-FORWARD (lines 1219-1244): ENDFILE sets ``NO MORE RECORDS TO SHOW``, and page 1
    with no row read sets ``NO RECORDS FOUND FOR THIS SEARCH CONDITION.``.
- The READNEXT and READPREV WHEN OTHER branches (lines 1222-1230, 1246-1254, 1308-1317,
    1361-1369) move WS-FILE-ERROR-MESSAGE (lines 153-171), READ on CARDDAT with the condition's
    RESP and RESP2, to WS-ERROR-MSG: a 503 file error on the account filter, either direction.
- 1400-SETUP-MESSAGE (lines 895-930), after 1100-SCREEN-INIT blanks WS-INFO-MSG: F7 that ends
    on page 1 (SUBTRACT 1 from page 2, or F7 already there) sets ``NO PREVIOUS PAGES TO
    DISPLAY``; F8 with no next page once the last page was shown sets ``NO MORE PAGES TO
    DISPLAY``; neither shows the information line ``TYPE S FOR DETAIL, U TO UPDATE ANY RECORD``,
    which every other page shows.
- 2250-EDIT-ARRAY (lines 1079-1115): more than one S or U gives ``PLEASE SELECT ONLY ONE RECORD
    TO VIEW OR UPDATE``; any other non-blank code, lower case included, ``INVALID ACTION CODE``.
    1250-SETUP-ARRAY-ATTRIBS puts the cursor on the first flagged row from row 2 on, or on
    ACCTSID. One S or U transfers to COCRDSLC (view) or COCRDUPC (update) with the row's card.

The program header promises admins every card, but the code only filters (ported unchanged),
so user types A and U list alike. Rows carry the card number masked and inside a card_ref,
never raw and never with the CVV. CCLI only reads: every case asserts cards.cards is unchanged.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NamedTuple
from unittest.mock import ANY, AsyncMock

import pytest
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError
from app.core.security import DecodeCardRef
from app.repositories.card_repository import (
    BROWSE_BACKWARD,
    BROWSE_FORWARD,
    CARD_FILE_NAME,
    CardRepository,
)
from app.schemas.cards import (
    CardListQuery,
    CardListResponse,
    CardSelectionRequest,
    CardSelectionRow,
)
from app.schemas.common import Direction
from app.services.cards.cocrdlic import ListCards, OnlineContext, ProcessCardSelection

type ContextFactory = Callable[[str], Awaitable[OnlineContext]]

# WS-MAX-SCREEN-LINES (lines 177-178) and the 50 records of data/EBCDIC CARDDATA.PS.
PAGE_SIZE = 7
SEEDED_CARD_COUNT = 50
FIRST_PAGE = 1
SECOND_PAGE = 2
THIRD_PAGE = 3
# WS-CA-SCREEN-NUM PIC 9(1): a forward walk past page 9 would wrap, so it never needs more.
WALK_PAGE_LIMIT = 9
WALK_LIMIT_MESSAGE = "F8 kept showing full pages past the PIC 9(1) page limit"

# WS-ERROR-MSG literals and 88 levels (lines 117-126, 903, 908, 1022, 1058, 1219, 1243).
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
NO_PREVIOUS_PAGES_MESSAGE = "NO PREVIOUS PAGES TO DISPLAY"
NO_MORE_PAGES_MESSAGE = "NO MORE PAGES TO DISPLAY"
NO_MORE_RECORDS_MESSAGE = "NO MORE RECORDS TO SHOW"
NO_RECORDS_FOUND_MESSAGE = "NO RECORDS FOUND FOR THIS SEARCH CONDITION."
MORE_THAN_ONE_MESSAGE = "PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE"
INVALID_ACTION_CODE_MESSAGE = "INVALID ACTION CODE"
# 88 WS-INFORM-REC-ACTIONS (lines 115-116); 88 WS-NO-INFO-MESSAGE is spaces.
RECORD_ACTIONS_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD"
NO_MESSAGE = ""
EDIT_FAILURE_STATUS = 400
FILE_ERROR_STATUS = 503

# WS-FILE-ERROR-MESSAGE (lines 153-171): FILLER X(12) 'File Error:', ERROR-OPNAME X(8), ' on ',
# ERROR-FILE X(9), ' returned RESP ', ERROR-RESP X(10), ',RESP2 ', ERROR-RESP2 X(10), FILLER X(5).
FILE_ERROR_LABEL = "File Error:"
FILE_ERROR_LABEL_WIDTH = 12
ERROR_OPNAME_WIDTH = 8
ERROR_FILE_WIDTH = 9
ERROR_RESP_WIDTH = 10
FILE_ERROR_FILLER_WIDTH = 5
# Every CARDDAT WHEN OTHER branch moves 'READ' and LIT-CARD-FILE X(8) (lines 213-214, 1226-1227).
READ_OPNAME = "READ"
LIT_CARD_FILE = "CARDDAT "
# WS-ERROR-MSG PIC X(75) (line 117), which receives the 80-byte group.
ERROR_MSG_LENGTH = 75
# That text for CICS IOERR (RESP 17, RESP2 0), as the message line shows it.
CARD_FILE_IOERR_MESSAGE = (
    "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000"
)

# ACCTSID and CARDSID, and the CRDSEL1-7 code of a row as the selection request names it.
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
SELECTION_FIELD_FORMAT = "rows[{}].selection"
SELECTION_ERROR_BODY_KEYS = {
    "message",
    "info_message",
    "field_errors",
    "cursor_field",
    "rows",
    "first_key",
    "last_key",
}

# A seeded account with a single card, and an account no seeded card names.
FILTERED_ACCT_ID = "00000000004"
UNKNOWN_ACCT_ID = "00000000099"
NON_NUMERIC_ACCT_TEXT = "abc"
NON_NUMERIC_CARD_TEXT = "xyz"

# 88 SELECT-OK VALUES 'S', 'U'; SELECT-BLANK is a space or an unsent field.
VIEW_CODE = "S"
UPDATE_CODE = "U"
BLANK_CODE = ""
SELECTED_ROW = 4

ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
EBCDIC_CODEC = "cp037"
MASK_PREFIX = "*" * 12
RAW_PAN_PATTERN = re.compile(r"\d{16}")
CVV_KEY_FRAGMENT = "cvv"

CARD_KEYS_SQL = "SELECT card_num, acct_id FROM cards.cards"
CARD_TABLE_SQL = "SELECT * FROM cards.cards ORDER BY card_num"


class ListScreen(NamedTuple):
    """What one COCRDLI page shows: its cards in row order, page number, lines and cursor."""

    cardNums: list[str]
    pageNumber: int
    message: str
    infoMessage: str
    fieldErrors: tuple[str, ...] = ()
    cursorField: str = ACCT_ID_FIELD


class FilterErrorCase(NamedTuple):
    """Filter text keyed in ACCTSID and CARDSID, and the refusal 2200-EDIT-INPUTS gives it."""

    acctText: str
    cardText: str
    expectedMessage: str
    failedFields: tuple[str, ...]


class SelectionErrorCase(NamedTuple):
    """CRDSEL1-7 codes keyed on page 1 and the refusal 2250-EDIT-ARRAY gives them."""

    codes: tuple[str, ...]
    expectedMessage: str
    flaggedRows: tuple[int, ...]
    cursorRow: int


class TransferCase(NamedTuple):
    """A single CRDSELn code and the program COCRDLIC transfers to, as the selection action."""

    code: str
    expectedAction: str


class BrowseErrorCase(NamedTuple):
    """The list request CARDDAT fails on, built with the real file, and the browse it runs."""

    failingRequest: Callable[[OnlineContext], Awaitable[CardListQuery]]
    expectedDirection: str


FILTER_ERROR_CASES = [
    pytest.param(
        FilterErrorCase(NON_NUMERIC_ACCT_TEXT, "", ACCOUNT_FILTER_MESSAGE, (ACCT_ID_FIELD,)),
        id="account-filter",
    ),
    pytest.param(
        FilterErrorCase("", NON_NUMERIC_CARD_TEXT, CARD_FILTER_MESSAGE, (CARD_NUM_FIELD,)),
        id="card-filter",
    ),
    pytest.param(
        FilterErrorCase(
            NON_NUMERIC_ACCT_TEXT,
            NON_NUMERIC_CARD_TEXT,
            ACCOUNT_FILTER_MESSAGE,
            (ACCT_ID_FIELD, CARD_NUM_FIELD),
        ),
        id="both-filters",
    ),
]

SELECTION_ERROR_CASES = [
    pytest.param(
        SelectionErrorCase(("", "S", "", "S", "", "", ""), MORE_THAN_ONE_MESSAGE, (1, 3), 1),
        id="two-view-codes",
    ),
    pytest.param(
        SelectionErrorCase(("U", "", "S", "", "", "", ""), MORE_THAN_ONE_MESSAGE, (0, 2), 2),
        id="update-and-view-codes-row-one-without-cursor",
    ),
    pytest.param(
        SelectionErrorCase(("", "", "X", "", "", "", ""), INVALID_ACTION_CODE_MESSAGE, (2,), 2),
        id="invalid-code",
    ),
    pytest.param(
        SelectionErrorCase(("", "s", "", "", "", "", ""), INVALID_ACTION_CODE_MESSAGE, (1,), 1),
        id="lower-case-view-code",
    ),
]

TRANSFER_CASES = [
    pytest.param(TransferCase(VIEW_CODE, "view"), id="view-to-cocrdslc"),
    pytest.param(TransferCase(UPDATE_CODE, "update"), id="update-to-cocrdupc"),
]


def ExpectedCardOrder(syncEngine: Engine, acctId: str | None = None) -> list[str]:
    """Return the seeded card numbers of ``acctId`` (all cards when None) in EBCDIC key order."""
    with syncEngine.connect() as connection:
        cardKeys = connection.execute(text(CARD_KEYS_SQL)).mappings().all()
    cardNums = [card["card_num"] for card in cardKeys if acctId in (None, card["acct_id"])]
    return sorted(cardNums, key=lambda cardNum: cardNum.encode(EBCDIC_CODEC))


def ReadCardTable(syncEngine: Engine) -> list[dict[str, Any]]:
    """Return every cards.cards row with every column, in card number order."""
    with syncEngine.connect() as connection:
        rows = connection.execute(text(CARD_TABLE_SQL)).mappings().all()
    return [dict(row) for row in rows]


class SeededCardFile:
    """CARDDAT as the test database holds it: expected key order and the rows as seeded."""

    def __init__(self, syncEngine: Engine) -> None:
        self.syncEngine = syncEngine
        self.seededRows = ReadCardTable(syncEngine)

    def CardOrder(self, acctId: str | None = None) -> list[str]:
        """Return the card numbers of ``acctId`` (all cards when None) in EBCDIC key order."""
        return ExpectedCardOrder(self.syncEngine, acctId)

    def CurrentRows(self) -> list[dict[str, Any]]:
        """Return cards.cards as it stands now, to compare with ``seededRows``."""
        return ReadCardTable(self.syncEngine)


def ChunkPages(cardNums: Sequence[str]) -> list[list[str]]:
    """Split ``cardNums`` into the 7-card pages F8 shows them on."""
    pageStarts = range(0, len(cardNums), PAGE_SIZE)
    return [list(cardNums[start : start + PAGE_SIZE]) for start in pageStarts]


def MaskOf(cardNum: str) -> str:
    """Return the CRDNUMn mask of ``cardNum``: twelve asterisks and its last four digits."""
    return MASK_PREFIX + cardNum[-4:]


def SelectionFieldName(rowIndex: int) -> str:
    """Return the field_errors name of the CRDSELn code of the 0-based row ``rowIndex``."""
    return SELECTION_FIELD_FORMAT.format(rowIndex)


def ScreenOf(page: CardListResponse, context: OnlineContext) -> ListScreen:
    """Return what ``page`` shows, each row's card number decoded from its card_ref."""
    return ListScreen(
        cardNums=[DecodeCardRef(row.card_ref, context.settings) for row in page.rows],
        pageNumber=page.page,
        message=page.message,
        infoMessage=page.info_message,
        fieldErrors=tuple(page.field_errors),
        cursorField=page.cursor_field,
    )


def RecordActionsScreen(cardNums: list[str], pageNumber: int, message: str) -> ListScreen:
    """Return a page shown with the WS-INFORM-REC-ACTIONS information line."""
    return ListScreen(cardNums, pageNumber, message, RECORD_ACTIONS_INFO)


def ExpectedForwardScreens(expectedPages: Sequence[list[str]]) -> list[ListScreen]:
    """Return the pages ENTER and F8 show: no message until ENDFILE on the last page."""
    lastIndex = len(expectedPages) - 1
    return [
        RecordActionsScreen(
            cardNums, index + 1, NO_MORE_RECORDS_MESSAGE if index == lastIndex else NO_MESSAGE
        )
        for index, cardNums in enumerate(expectedPages)
    ]


def PagingQuery(page: CardListResponse, direction: Direction) -> CardListQuery:
    """Return the query COCRDLI sends from ``page``: F8 echoes last_key, F7 first_key."""
    startKey = page.last_key if direction == Direction.FORWARD else page.first_key
    return CardListQuery(start_key=startKey, direction=direction, page=page.page)


async def PageForward(context: OnlineContext, page: CardListResponse) -> CardListResponse:
    """Press F8 on ``page``."""
    return await ListCards(context, PagingQuery(page, Direction.FORWARD))


async def PageBackward(context: OnlineContext, page: CardListResponse) -> CardListResponse:
    """Press F7 on ``page``."""
    return await ListCards(context, PagingQuery(page, Direction.BACKWARD))


async def WalkForward(
    context: OnlineContext, firstPage: CardListResponse
) -> list[CardListResponse]:
    """Press F8 from ``firstPage`` until a page is short or hits ENDFILE; return every page."""
    pages = [firstPage]
    while len(pages[-1].rows) == PAGE_SIZE and pages[-1].message != NO_MORE_RECORDS_MESSAGE:
        assert len(pages) < WALK_PAGE_LIMIT, WALK_LIMIT_MESSAGE
        pages.append(await PageForward(context, pages[-1]))
    return pages


def SelectionRequest(listedPage: CardListResponse, codes: Sequence[str]) -> CardSelectionRequest:
    """Return ENTER on ``listedPage`` with ``codes`` keyed in CRDSEL1-7, row by row."""
    rows = [
        CardSelectionRow(card_ref=row.card_ref, selection=code)
        for row, code in zip(listedPage.rows, codes, strict=True)
    ]
    return CardSelectionRequest(rows=rows)


def CodesSelecting(code: str, rowIndex: int) -> tuple[str, ...]:
    """Return seven CRDSELn codes, blank except ``code`` on the 0-based row ``rowIndex``."""
    return tuple(code if index == rowIndex else BLANK_CODE for index in range(PAGE_SIZE))


def ExpectedFileErrorMessage(error: FileIoError) -> str:
    """Return WS-ERROR-MSG after a CARDDAT browse WHEN OTHER branch ends in ``error``.

    'READ' and LIT-CARD-FILE fill the space-padded ERROR-OPNAME X(8) and ERROR-FILE X(9).
    WS-RESP-CD and WS-REAS-CD, both S9(09) COMP (lines 47-50), moved to the X(10) ERROR-RESP and
    ERROR-RESP2 give nine digits and a space. The 80-byte group is cut to WS-ERROR-MSG X(75),
    whose trailing spaces are not shown.
    """
    fileErrorGroup = (
        FILE_ERROR_LABEL.ljust(FILE_ERROR_LABEL_WIDTH)
        + READ_OPNAME.ljust(ERROR_OPNAME_WIDTH)
        + " on "
        + LIT_CARD_FILE.ljust(ERROR_FILE_WIDTH)
        + " returned RESP "
        + f"{error.resp:09d}".ljust(ERROR_RESP_WIDTH)
        + ",RESP2 "
        + f"{error.resp2:09d}".ljust(ERROR_RESP_WIDTH)
        + " " * FILE_ERROR_FILLER_WIDTH
    )
    return fileErrorGroup[:ERROR_MSG_LENGTH].rstrip(" ")


async def FreshEntryRequest(context: OnlineContext) -> CardListQuery:
    """Return ENTER on a fresh entry, which 9000-READ-FORWARD answers from the lowest key."""
    return CardListQuery()


async def SecondPageBackwardRequest(context: OnlineContext) -> CardListQuery:
    """List page 1, press F8, and return F7 on page 2, which 9100-READ-BACKWARDS answers."""
    secondPage = await PageForward(context, await ListCards(context, CardListQuery()))
    return PagingQuery(secondPage, Direction.BACKWARD)


# The READNEXT branches (lines 1222-1230, 1246-1254) and the READPREV ones (1308-1317, 1361-1369).
BROWSE_ERROR_CASES = [
    pytest.param(BrowseErrorCase(FreshEntryRequest, BROWSE_FORWARD), id="readnext-forward"),
    pytest.param(
        BrowseErrorCase(SecondPageBackwardRequest, BROWSE_BACKWARD), id="readprev-backward"
    ),
]


@pytest.fixture(name="cardFile")
def SeededCardFileFixture(migratedDatabase, syncEngine: Engine) -> SeededCardFile:
    """Return the seeded CARDDAT, so each case can show CCLI wrote nothing to it."""
    return SeededCardFile(syncEngine)


async def test_initial_list_shows_first_seven_cards_masked(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """ENTER on a fresh entry shows the first 7 cards in key order, masked, with no CVV."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    firstPage = await ListCards(context, CardListQuery())
    expectedCards = cardFile.CardOrder()[:PAGE_SIZE]
    assert ScreenOf(firstPage, context) == RecordActionsScreen(
        expectedCards, FIRST_PAGE, NO_MESSAGE
    )
    assert [row.card_num_masked for row in firstPage.rows] == [
        MaskOf(cardNum) for cardNum in expectedCards
    ]
    for row in firstPage.rows:
        assert not [key for key in row.model_dump() if CVV_KEY_FRAGMENT in key]
    assert RAW_PAN_PATTERN.search(firstPage.model_dump_json()) is None
    assert cardFile.CurrentRows() == cardFile.seededRows


async def test_page_forward_shows_every_card_once_in_ebcdic_order(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """F8 from page 1 shows all 50 cards on 7 x 7 + 1 rows; F8 once more has no more pages."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    expectedOrder = cardFile.CardOrder()
    expectedPages = ChunkPages(expectedOrder)
    pages = await WalkForward(context, await ListCards(context, CardListQuery()))
    assert len(expectedOrder) == SEEDED_CARD_COUNT
    assert [ScreenOf(page, context) for page in pages] == ExpectedForwardScreens(expectedPages)
    pastLastPage = await PageForward(context, pages[-1])
    assert ScreenOf(pastLastPage, context) == ListScreen(
        expectedPages[-1], len(expectedPages), NO_MORE_PAGES_MESSAGE, NO_MESSAGE
    )
    assert cardFile.CurrentRows() == cardFile.seededRows


async def test_page_backward_returns_previous_pages_down_to_the_first(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """F7 on page 3 shows page 2; F7 that ends on page 1, or is pressed there, has no previous."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    expectedPages = ChunkPages(cardFile.CardOrder())
    firstPage = await ListCards(context, CardListQuery())
    thirdPage = await PageForward(context, await PageForward(context, firstPage))
    secondPage = await PageBackward(context, thirdPage)
    backToFirstPage = await PageBackward(context, secondPage)
    stillFirstPage = await PageBackward(context, backToFirstPage)
    assert ScreenOf(thirdPage, context) == RecordActionsScreen(
        expectedPages[2], THIRD_PAGE, NO_MESSAGE
    )
    assert [ScreenOf(page, context) for page in (secondPage, backToFirstPage, stillFirstPage)] == [
        RecordActionsScreen(expectedPages[1], SECOND_PAGE, NO_MESSAGE),
        ListScreen(expectedPages[0], FIRST_PAGE, NO_PREVIOUS_PAGES_MESSAGE, NO_MESSAGE),
        ListScreen(expectedPages[0], FIRST_PAGE, NO_PREVIOUS_PAGES_MESSAGE, NO_MESSAGE),
    ]
    assert cardFile.CurrentRows() == cardFile.seededRows


async def test_account_filter_lists_only_that_accounts_cards(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """9500-FILTER-RECORDS keeps the account's cards; the short page reaches ENDFILE."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    expectedCards = cardFile.CardOrder(FILTERED_ACCT_ID)
    filteredPage = await ListCards(context, CardListQuery(acct_id=FILTERED_ACCT_ID))
    assert 0 < len(expectedCards) < PAGE_SIZE
    assert ScreenOf(filteredPage, context) == RecordActionsScreen(
        expectedCards, FIRST_PAGE, NO_MORE_RECORDS_MESSAGE
    )
    assert {row.acct_id for row in filteredPage.rows} == {FILTERED_ACCT_ID}
    assert cardFile.CurrentRows() == cardFile.seededRows


async def test_filter_matching_no_card_finds_no_records(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """Page 1 with no row read sets WS-NO-RECORDS-FOUND, which hides the information line."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    emptyPage = await ListCards(context, CardListQuery(acct_id=UNKNOWN_ACCT_ID))
    assert cardFile.CardOrder(UNKNOWN_ACCT_ID) == []
    assert ScreenOf(emptyPage, context) == ListScreen(
        [], FIRST_PAGE, NO_RECORDS_FOUND_MESSAGE, NO_MESSAGE
    )
    assert cardFile.CurrentRows() == cardFile.seededRows


@pytest.mark.parametrize("case", FILTER_ERROR_CASES)
async def test_non_numeric_filter_is_refused_without_rows(
    case: FilterErrorCase,
    makeOnlineContext: ContextFactory,
    cardFile: SeededCardFile,
) -> None:
    """A non-numeric filter fails its edit: 400, every failing field, the first one's literal."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    query = CardListQuery(acct_id=case.acctText, card_num=case.cardText)
    with pytest.raises(LegacyMessageError) as refusal:
        await ListCards(context, query)
    assert refusal.value.httpStatus == EDIT_FAILURE_STATUS
    assert refusal.value.Body() == {
        "message": case.expectedMessage,
        "info_message": NO_MESSAGE,
        "field_errors": list(case.failedFields),
        "cursor_field": case.failedFields[0],
    }
    assert cardFile.CurrentRows() == cardFile.seededRows


@pytest.mark.parametrize("case", BROWSE_ERROR_CASES)
async def test_card_file_browse_error_shows_the_read_error_text(
    case: BrowseErrorCase,
    makeOnlineContext: ContextFactory,
    cardFile: SeededCardFile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A READNEXT or READPREV ending WHEN OTHER is a 503 with the file error and the account."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    query = await case.failingRequest(context)
    ioError = FileIoError(CARD_FILE_NAME)
    browseCards = AsyncMock(side_effect=ioError)
    monkeypatch.setattr(CardRepository, "BrowseCards", browseCards)
    with pytest.raises(LegacyMessageError) as refusal:
        await ListCards(context, query)
    assert refusal.value.httpStatus == FILE_ERROR_STATUS
    assert refusal.value.Body() == {
        "message": ExpectedFileErrorMessage(ioError),
        "info_message": NO_MESSAGE,
        "field_errors": [ACCT_ID_FIELD],
        "cursor_field": ACCT_ID_FIELD,
    }
    assert ExpectedFileErrorMessage(ioError) == CARD_FILE_IOERR_MESSAGE
    browseCards.assert_awaited_once_with(ANY, ANY, case.expectedDirection, PAGE_SIZE)
    assert cardFile.CurrentRows() == cardFile.seededRows


@pytest.mark.parametrize("case", SELECTION_ERROR_CASES)
async def test_refused_selection_redisplays_the_first_page(
    case: SelectionErrorCase,
    makeOnlineContext: ContextFactory,
    cardFile: SeededCardFile,
) -> None:
    """A refused CRDSELn code is a 400 naming every flagged row, with page 1 redisplayed."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    listedPage = await ListCards(context, CardListQuery())
    with pytest.raises(LegacyMessageError) as refusal:
        await ProcessCardSelection(context, SelectionRequest(listedPage, case.codes))
    body = refusal.value.Body()
    assert refusal.value.httpStatus == EDIT_FAILURE_STATUS
    assert set(body) == SELECTION_ERROR_BODY_KEYS
    assert (body["message"], body["info_message"]) == (case.expectedMessage, RECORD_ACTIONS_INFO)
    assert body["field_errors"] == [SelectionFieldName(row) for row in case.flaggedRows]
    assert body["cursor_field"] == SelectionFieldName(case.cursorRow)
    redisplayedCards = [DecodeCardRef(row["card_ref"], context.settings) for row in body["rows"]]
    assert redisplayedCards == cardFile.CardOrder()[:PAGE_SIZE]
    assert cardFile.CurrentRows() == cardFile.seededRows


@pytest.mark.parametrize("case", TRANSFER_CASES)
async def test_single_selection_transfers_with_the_rows_card(
    case: TransferCase,
    makeOnlineContext: ContextFactory,
    cardFile: SeededCardFile,
    testSettings,
) -> None:
    """One S or U returns its action and a card_ref holding that row's raw card number."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    listedPage = await ListCards(context, CardListQuery())
    codes = CodesSelecting(case.code, SELECTED_ROW)
    transfer = await ProcessCardSelection(context, SelectionRequest(listedPage, codes))
    assert (transfer.action, transfer.message) == (case.expectedAction, NO_MESSAGE)
    expectedCard = cardFile.CardOrder()[SELECTED_ROW]
    assert DecodeCardRef(transfer.card_ref, testSettings) == expectedCard
    assert listedPage.rows[SELECTED_ROW].card_num_masked == MaskOf(expectedCard)
    assert cardFile.CurrentRows() == cardFile.seededRows


async def test_admin_and_regular_users_list_the_same_cards(
    makeOnlineContext: ContextFactory, cardFile: SeededCardFile
) -> None:
    """The header's admin all-cards mode does not exist: types A and U see one first page."""
    adminContext = await makeOnlineContext(ADMIN_USER_TYPE)
    regularContext = await makeOnlineContext(REGULAR_USER_TYPE)
    adminPage = await ListCards(adminContext, CardListQuery())
    regularPage = await ListCards(regularContext, CardListQuery())
    expectedScreen = RecordActionsScreen(cardFile.CardOrder()[:PAGE_SIZE], FIRST_PAGE, NO_MESSAGE)
    assert ScreenOf(adminPage, adminContext) == expectedScreen
    assert ScreenOf(regularPage, regularContext) == expectedScreen
    assert [row.model_dump(exclude={"card_ref"}) for row in adminPage.rows] == [
        row.model_dump(exclude={"card_ref"}) for row in regularPage.rows
    ]
    assert cardFile.CurrentRows() == cardFile.seededRows
