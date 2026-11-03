"""Unit tests for the COCRDLIC (CCLI) selection edit on the GET /cards paging keys.

cbl/COCRDLIC.cbl receives the map on ENTER, F7 and F8 alike: 2000-RECEIVE-MAP performs
2200-EDIT-INPUTS, whose 2250-EDIT-ARRAY edits CRDSEL1-7 (lines 1073-1117). The EVALUATE's first
arm, WHEN INPUT-ERROR (lines 419-438), precedes every paging arm, so a refused code redisplays
the screen instead of paging. That arm performs 9000-READ-FORWARD from the INITIALIZEd
WS-CARD-RID-CARDNUM, the lowest key, with the COMMAREA page number and paging flags:
CA-LAST-PAGE-SHOWN is reset unless PF8 was pressed (lines 410-414), and a row read on page 0
makes it page 1 (lines 1177-1181). 1400-SETUP-MESSAGE (lines 895-930) then runs with the key
actually pressed. Expected values are typed from those lines:
- more than one S or U gives ``PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE`` and flags
    both rows; any other non-blank code, lower-case ``s`` included, ``INVALID ACTION CODE``;
- the cursor goes to the first flagged row from row 2 on (1250-SETUP-ARRAY-ATTRIBS), or to
    ACCTSID when only row 1 is flagged;
- F7 on page 1 overrides the literal with ``NO PREVIOUS PAGES TO DISPLAY``, and F8 with no
    next page once the last page was shown with ``NO MORE PAGES TO DISPLAY``, neither with the
    information line;
- a single S or U on F7 or F8 never transfers (the XCTL arms test CCARD-AID-ENTER): the key
    pages exactly as with blank codes;
- POST /cards/selection keeps its first-entry redisplay on page 1 with no ``page`` in its body.

app.services.cards.cocrdlic's CardRepository is replaced by a stub whose BrowseCards is an
AsyncMock, so no database is needed.
"""

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.core.security import DecodeCardRef
from app.repositories.card_repository import CardFilters
from app.schemas.cards import (
    CardListQuery,
    CardListResponse,
    CardSelectionRequest,
    CardSelectionRow,
)
from app.services.cards import cocrdlic
from app.services.cards.cocrdlic import DecodePageKey, EncodePageKey, PageCursor

# WS-MAX-SCREEN-LINES (lines 177-178); a forward browse appends the look-ahead record after it.
PAGE_SIZE = 7
FULL_PAGE_WITH_LOOK_AHEAD = PAGE_SIZE + 1
SHORT_PAGE = 5
# The literals of lines 903, 908, 1086 and 1112, and 88 WS-INFORM-REC-ACTIONS (lines 115-116).
MORE_THAN_ONE_MESSAGE = "PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE"
INVALID_ACTION_MESSAGE = "INVALID ACTION CODE"
NO_PREVIOUS_PAGES_MESSAGE = "NO PREVIOUS PAGES TO DISPLAY"
NO_MORE_PAGES_MESSAGE = "NO MORE PAGES TO DISPLAY"
RECORD_ACTIONS_INFO = "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD"
NO_MESSAGE = ""
REFUSAL_STATUS = 400
# ACCTSID, the cursor when no row from 2 to 7 is flagged.
ACCT_ID_FIELD = "acct_id"
FORWARD = "forward"
BACKWARD = "backward"
# The masked CRDNUMn prefix: twelve asterisks before the last four digits.
MASK_PREFIX = "*" * 12
# Synthetic card numbers, assembled from parts: twelve leading digits and a 4-digit row number.
CARD_NUM_LEAD = "4" + "0" * 11
# Blank filters: 9500-FILTER-RECORDS applies neither; LOW-VALUES reaches the stub as None.
NO_FILTERS = CardFilters(acctId=None, cardNum=None)
LOW_VALUES_KEY = None
# The page shown when the key is pressed: cards 8 to 14, with card 15 as the look-ahead.
SHOWN_FIRST_NUMBER = 8
LOOK_AHEAD_NUMBER = 15
NEXT_PAGE_FIRST_NUMBER = 15
PREVIOUS_PAGE_FIRST_NUMBER = 1
LOWEST_NUMBER = 1
# The POST /cards/selection refusal body: the four legacy keys, the rows and both page keys.
SELECTION_POST_BODY_KEYS = {
    "message",
    "info_message",
    "field_errors",
    "cursor_field",
    "rows",
    "first_key",
    "last_key",
}


def CardNumber(number: int) -> str:
    """Return the 16-character synthetic card number of card ``number``."""
    return f"{CARD_NUM_LEAD}{number:04d}"


SHOWN_FIRST_CARD = CardNumber(SHOWN_FIRST_NUMBER)
LOOK_AHEAD_CARD = CardNumber(LOOK_AHEAD_NUMBER)


class StubCardRepository:
    """CardRepository's CARDDAT browse as an AsyncMock."""

    def __init__(self) -> None:
        self.BrowseCards = AsyncMock(return_value=[])

    def ForContext(self, context: OnlineContext) -> "StubCardRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


class KeyPress(NamedTuple):
    """A paging key on COCRDLI: direction, page shown, CA-LAST-PAGE-SHOWN and the row codes."""

    direction: str
    page: int
    lastPageShown: bool
    selections: tuple[str, ...]


class RefusedPagingCase(NamedTuple):
    """A refused key press, the records its redisplay reads and the 400 it answers."""

    press: KeyPress
    browseRecords: int
    expectedMessage: str
    expectedInfo: str
    flaggedRows: tuple[int, ...]
    expectedCursorField: str
    expectedPage: int
    expectedLastPageShown: bool


class PagingCase(NamedTuple):
    """An accepted key press, the browse it starts and the page it shows."""

    press: KeyPress
    browseStart: str
    browseDirection: str
    browseFirstNumber: int
    browseRecords: int
    expectedPage: int
    expectedMessage: str


def RowCodes(codesByRow: Mapping[int, str]) -> tuple[str, ...]:
    """Return the seven CRDSELn codes, blank except the 0-based rows given."""
    return tuple(codesByRow.get(rowIndex, "") for rowIndex in range(PAGE_SIZE))


def SelectionField(rowIndex: int) -> str:
    """Return the field_errors name of a row's selection code."""
    return f"rows[{rowIndex}].selection"


REFUSED_PAGING_CASES = [
    RefusedPagingCase(
        KeyPress(FORWARD, 2, False, RowCodes({0: "S", 1: "U"})),
        FULL_PAGE_WITH_LOOK_AHEAD,
        MORE_THAN_ONE_MESSAGE,
        RECORD_ACTIONS_INFO,
        (0, 1),
        SelectionField(1),
        2,
        False,
    ),
    RefusedPagingCase(
        KeyPress(BACKWARD, 3, False, RowCodes({0: "S", 1: "U"})),
        FULL_PAGE_WITH_LOOK_AHEAD,
        MORE_THAN_ONE_MESSAGE,
        RECORD_ACTIONS_INFO,
        (0, 1),
        SelectionField(1),
        3,
        False,
    ),
    RefusedPagingCase(
        KeyPress(FORWARD, 2, False, RowCodes({2: "X"})),
        FULL_PAGE_WITH_LOOK_AHEAD,
        INVALID_ACTION_MESSAGE,
        RECORD_ACTIONS_INFO,
        (2,),
        SelectionField(2),
        2,
        False,
    ),
    RefusedPagingCase(
        KeyPress(BACKWARD, 2, True, RowCodes({0: "s"})),
        FULL_PAGE_WITH_LOOK_AHEAD,
        INVALID_ACTION_MESSAGE,
        RECORD_ACTIONS_INFO,
        (0,),
        ACCT_ID_FIELD,
        2,
        False,
    ),
    RefusedPagingCase(
        KeyPress(BACKWARD, 1, False, RowCodes({3: "S", 5: "U"})),
        FULL_PAGE_WITH_LOOK_AHEAD,
        NO_PREVIOUS_PAGES_MESSAGE,
        NO_MESSAGE,
        (3, 5),
        SelectionField(3),
        1,
        False,
    ),
    RefusedPagingCase(
        KeyPress(FORWARD, 2, True, RowCodes({0: "U", 6: "U"})),
        SHORT_PAGE,
        NO_MORE_PAGES_MESSAGE,
        NO_MESSAGE,
        (0, 6),
        SelectionField(6),
        2,
        True,
    ),
    RefusedPagingCase(
        KeyPress(FORWARD, 0, False, RowCodes({1: "X"})),
        SHORT_PAGE,
        INVALID_ACTION_MESSAGE,
        RECORD_ACTIONS_INFO,
        (1,),
        SelectionField(1),
        1,
        True,
    ),
]
REFUSED_PAGING_IDS = [
    "F8-next-page-S-and-U",
    "F7-page3-S-and-U",
    "F8-invalid-X",
    "F7-lower-case-s-row1",
    "F7-page1-S-and-U",
    "F8-last-page-shown-no-next-page",
    "F8-page0-invalid-X",
]

PAGING_CASES = [
    PagingCase(
        KeyPress(FORWARD, 2, False, ()),
        LOOK_AHEAD_CARD,
        FORWARD,
        NEXT_PAGE_FIRST_NUMBER,
        FULL_PAGE_WITH_LOOK_AHEAD,
        3,
        NO_MESSAGE,
    ),
    PagingCase(
        KeyPress(FORWARD, 2, False, RowCodes({1: " "})),
        LOOK_AHEAD_CARD,
        FORWARD,
        NEXT_PAGE_FIRST_NUMBER,
        FULL_PAGE_WITH_LOOK_AHEAD,
        3,
        NO_MESSAGE,
    ),
    PagingCase(
        KeyPress(FORWARD, 2, False, RowCodes({1: "S"})),
        LOOK_AHEAD_CARD,
        FORWARD,
        NEXT_PAGE_FIRST_NUMBER,
        FULL_PAGE_WITH_LOOK_AHEAD,
        3,
        NO_MESSAGE,
    ),
    PagingCase(
        KeyPress(BACKWARD, 3, False, RowCodes({0: "U"})),
        SHOWN_FIRST_CARD,
        BACKWARD,
        PREVIOUS_PAGE_FIRST_NUMBER,
        PAGE_SIZE,
        2,
        NO_MESSAGE,
    ),
    PagingCase(
        KeyPress(BACKWARD, 1, False, RowCodes({4: "S"})),
        SHOWN_FIRST_CARD,
        FORWARD,
        SHOWN_FIRST_NUMBER,
        FULL_PAGE_WITH_LOOK_AHEAD,
        1,
        NO_PREVIOUS_PAGES_MESSAGE,
    ),
]
PAGING_IDS = [
    "F8-no-codes",
    "F8-blank-codes",
    "F8-single-S",
    "F7-single-U",
    "F7-page1-single-S",
]


@pytest.fixture(name="cardRepository")
def CardRepositoryStub(monkeypatch) -> StubCardRepository:
    """Make cocrdlic build the stub wherever it builds a CardRepository, and return the stub."""
    stubRepository = StubCardRepository()
    monkeypatch.setattr(cocrdlic, "CardRepository", stubRepository.ForContext)
    return stubRepository


def BuildContext(settings) -> OnlineContext:
    """Return an online context with the settings the page keys and card_ref tokens need."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=settings)


def CardRecords(firstNumber: int, count: int) -> list[dict[str, str]]:
    """Return ``count`` CARDDAT records in the order the browse returns them."""
    return [
        {"card_num": CardNumber(number), "acct_id": f"{number:011d}", "active_status": "Y"}
        for number in range(firstNumber, firstNumber + count)
    ]


def MaskedCells(firstNumber: int, count: int) -> list[dict[str, str]]:
    """Return the ACCTNOn, masked CRDNUMn and CRDSTSn cells of the rows shown."""
    return [
        {
            "acct_id": f"{number:011d}",
            "card_num_masked": f"{MASK_PREFIX}{number:04d}",
            "active_status": "Y",
        }
        for number in range(firstNumber, firstNumber + count)
    ]


def ShownRowCells(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return each response row without its card_ref token, which differs on every run."""
    return [{key: value for key, value in row.items() if key != "card_ref"} for row in rows]


def PagingQuery(settings, press: KeyPress) -> CardListQuery:
    """Return the GET /cards query of ``press`` on the page of cards 8 to 14."""
    shownCursor = PageCursor(
        firstCardKey=SHOWN_FIRST_CARD,
        lastCardKey=LOOK_AHEAD_CARD,
        nextPageExists=True,
        lastPageShown=press.lastPageShown,
    )
    return CardListQuery(
        startKey=EncodePageKey(shownCursor, settings),
        direction=press.direction,
        page=press.page,
        selections=list(press.selections),
    )


def ExpectedRefusalFeedback(refusal: RefusedPagingCase) -> dict[str, object]:
    """Return the four legacy keys and the page of a refused key press."""
    return {
        "message": refusal.expectedMessage,
        "info_message": refusal.expectedInfo,
        "field_errors": [SelectionField(rowIndex) for rowIndex in refusal.flaggedRows],
        "cursor_field": refusal.expectedCursorField,
        "page": refusal.expectedPage,
    }


def RedisplayCursor(refusal: RefusedPagingCase) -> PageCursor:
    """Return the paging state after the redisplay read from the lowest key."""
    return PageCursor(
        firstCardKey=CardNumber(LOWEST_NUMBER),
        lastCardKey=CardNumber(refusal.browseRecords),
        nextPageExists=refusal.browseRecords > PAGE_SIZE,
        lastPageShown=refusal.expectedLastPageShown,
    )


@pytest.mark.parametrize("refusal", REFUSED_PAGING_CASES, ids=REFUSED_PAGING_IDS)
async def test_refused_codes_on_paging_key_redisplay_from_lowest_key(
    cardRepository, testSettings, refusal
):
    """WHEN INPUT-ERROR: the rows from the lowest key, the 400 literal, flags, keys and page."""
    cardRepository.BrowseCards.return_value = CardRecords(LOWEST_NUMBER, refusal.browseRecords)
    expectedFeedback = ExpectedRefusalFeedback(refusal)
    shownRecords = CardRecords(LOWEST_NUMBER, min(refusal.browseRecords, PAGE_SIZE))
    query = PagingQuery(testSettings, refusal.press)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdlic.ListCards(BuildContext(testSettings), query)
    body = raised.value.Body()
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert {key: body[key] for key in expectedFeedback} == expectedFeedback
    assert ShownRowCells(body["rows"]) == MaskedCells(LOWEST_NUMBER, len(shownRecords))
    assert [DecodeCardRef(row["card_ref"], testSettings) for row in body["rows"]] == [
        record["card_num"] for record in shownRecords
    ]
    assert DecodePageKey(body["first_key"], testSettings) == RedisplayCursor(refusal)
    assert DecodePageKey(body["last_key"], testSettings) == RedisplayCursor(refusal)
    cardRepository.BrowseCards.assert_awaited_once_with(
        NO_FILTERS, LOW_VALUES_KEY, FORWARD, PAGE_SIZE
    )


@pytest.mark.parametrize("paging", PAGING_CASES, ids=PAGING_IDS)
async def test_blank_or_single_code_pages_without_transfer(cardRepository, testSettings, paging):
    """No refused code: the key pages exactly as without codes, and no view or update starts."""
    records = CardRecords(paging.browseFirstNumber, paging.browseRecords)
    # READPREV returns the nearest card first; the page shows the cards in ascending order.
    isBackward = paging.browseDirection == BACKWARD
    cardRepository.BrowseCards.return_value = records[::-1] if isBackward else records
    query = PagingQuery(testSettings, paging.press)
    response = await cocrdlic.ListCards(BuildContext(testSettings), query)
    shownCells = ShownRowCells([row.model_dump() for row in response.rows])
    assert isinstance(response, CardListResponse)
    assert response.page == paging.expectedPage
    assert response.message == paging.expectedMessage
    assert shownCells == MaskedCells(paging.browseFirstNumber, min(len(records), PAGE_SIZE))
    cardRepository.BrowseCards.assert_awaited_once_with(
        NO_FILTERS, paging.browseStart, paging.browseDirection, PAGE_SIZE
    )


async def test_selection_post_redisplay_keeps_first_entry_without_page(
    cardRepository, testSettings
):
    """POST /cards/selection with S and U: redisplayed as ENTER on page 1, no page in the body."""
    cardRepository.BrowseCards.return_value = CardRecords(LOWEST_NUMBER, FULL_PAGE_WITH_LOOK_AHEAD)
    rows = [CardSelectionRow(selection=code) for code in RowCodes({0: "S", 2: "U"})]
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdlic.ProcessCardSelection(
            BuildContext(testSettings), CardSelectionRequest(rows=rows)
        )
    body = raised.value.Body()
    assert raised.value.httpStatus == REFUSAL_STATUS
    assert set(body) == SELECTION_POST_BODY_KEYS
    assert (body["message"], body["info_message"]) == (MORE_THAN_ONE_MESSAGE, RECORD_ACTIONS_INFO)
    assert body["field_errors"] == [SelectionField(0), SelectionField(2)]
    assert body["cursor_field"] == SelectionField(2)
    assert ShownRowCells(body["rows"]) == MaskedCells(LOWEST_NUMBER, PAGE_SIZE)
    cardRepository.BrowseCards.assert_awaited_once_with(
        NO_FILTERS, LOW_VALUES_KEY, FORWARD, PAGE_SIZE
    )
