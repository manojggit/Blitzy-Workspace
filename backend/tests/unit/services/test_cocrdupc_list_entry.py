"""Unit tests for the COCRDUPC (CCUP) entry from the card list and its terminal screen tokens.

cbl/COCRDUPC.cbl lines 482-497 (WHEN CDEMO-PGM-ENTER AND CDEMO-FROM-PROGRAM EQUAL
LIT-CCLISTPGM) set FLG-ACCTFILTER-ISVALID and FLG-CARDFILTER-ISVALID, move the account and
card COCRDLIC passed from the selected row (cbl/COCRDLIC.cbl lines 552-556) to CC-ACCT-ID-N and
CC-CARD-NUM-N, PERFORM 9000-READ-DATA and SET CCUP-SHOW-DETAILS: no 1210-EDIT-ACCOUNT or
1220-EDIT-CARD runs. The all-zero refusal of 1220-EDIT-CARD (lines 768-779) belongs to keys
typed on the COCRDUP screen. Expected values are typed from those lines and the 88 levels they
set:
- a list card_ref with its row account opens the card into SHOW_DETAILS with ``Details of
    selected card shown above`` and the cursor on the name (lines 160-161, 1212-1214), the
    all-zero card key and the all-zero account included; valid typed keys open it too;
- the all-zero card typed is ``Card number not provided`` (line 180);
- a card_ref with an account no list row carries runs 1210 (lines 178, 745);
- a forged card_ref, and one carrying a CCLI page key, give ``Did not find cards for this
    search condition`` (line 204);
- a done or failed (C, L, F) token keeps CCUP-NEW-CARDDATA, which 3200-SETUP-SCREEN-VARS shows
    for CCUP-CHANGES-MADE (lines 1113-1123).

app.services.cards.cocrdupc's CardRepository is replaced by a stub whose GetCard is an
AsyncMock, so no database is needed.
"""

import secrets
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.core.security import EncodeCardRef
from app.schemas.cards import CardScreenFields
from app.schemas.common import UpdateScreenState
from app.services.cards import cocrdupc

if TYPE_CHECKING:
    from app.core.config import Settings

# CARD-NUM X(16) and CARD-ACCT-ID 9(11) (copybook CVACT02Y): all zeros is a legal key of each.
ZERO_CARD_NUM = "0000000000000000"
LISTED_CARD_NUM = "0000000000004321"
LIST_ACCT_ID = "00000000004"
ZERO_ACCT_ID = "00000000000"
# Account texts no COCRDLI row carries: blank, short, and 11 characters that are not digits.
NO_ACCT_TEXT = ""
SHORT_ACCT_TEXT = "123"
NON_DIGIT_ACCT_TEXT = "0000000000A"
# A CCLI page key's payload: both X(16) keys and the two paging flags, 34 characters.
PAGE_KEY_PAYLOAD = LISTED_CARD_NUM + ZERO_CARD_NUM + "NN"
SECRET_KEY_BYTES = 48

# The CARDDAT record the stub reads, and CCUP-OLD-* as 9000-READ-DATA keeps it (lines 1354-1367).
EMBOSSED_NAME = "Zero Key Holder"
UPPER_EMBOSSED_NAME = "ZERO KEY HOLDER"
CARD_CVV = "123"
EXPIRATION_DATE = date(2027, 3, 31)
EXPIRY_YEAR = "2027"
EXPIRY_MONTH = "03"
EXPIRY_DAY = "31"
ACTIVE_STATUS = "Y"
NEW_NAME = "Zero Key Renamed"
NEW_STATUS = "N"
NEW_MONTH = "12"
NEW_YEAR = "2030"

# WS-RETURN-MSG and WS-INFO-MSG 88 levels (lines 160-161, 178, 180, 204, 745).
DETAILS_SHOWN_INFO = "Details of selected card shown above"
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
CARD_NOT_PROVIDED_MESSAGE = "Card number not provided"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
NO_MESSAGE = ""
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
NAME_FIELD = "embossed_name"
EDIT_FAILED_STATUS = 400
NOT_FOUND_STATUS = 404
# CCUP-CHANGES-OKAYED-AND-DONE, COULD-NOT-LOCK-FOR-UPDATE and LOCKED-BUT-UPDATE-FAILED.
FINISHED_STATE_CODES = ["C", "L", "F"]


def CardRecord(cardNum: str) -> dict[str, object]:
    """Return the CARDDAT record of ``cardNum`` as CardRepository.GetCard maps it."""
    return {
        "card_num": cardNum,
        "acct_id": LIST_ACCT_ID,
        "cvv_cd": CARD_CVV,
        "embossed_name": EMBOSSED_NAME,
        "expiration_date": EXPIRATION_DATE,
        "active_status": ACTIVE_STATUS,
    }


class StubCardRepository:
    """CardRepository's CARDDAT READ as an AsyncMock that finds every card it is asked for."""

    def __init__(self) -> None:
        self.GetCard = AsyncMock(side_effect=CardRecord)

    def ForContext(self, context: OnlineContext) -> "StubCardRepository":
        """Return this stub as the repository the service builds from its context."""
        return self


@pytest.fixture(name="cardRepository")
def CardRepositoryStub(monkeypatch) -> StubCardRepository:
    """Make cocrdupc build the stub wherever it builds a CardRepository, and return the stub."""
    stubRepository = StubCardRepository()
    monkeypatch.setattr(cocrdupc, "CardRepository", stubRepository.ForContext)
    return stubRepository


def BuildContext(settings: "Settings") -> OnlineContext:
    """Return an online context holding only the settings the card_ref and token ciphers use."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=settings)


def ForgedCardRef(settings: "Settings") -> str:
    """Return a card_ref of a listed card sealed under a SECRET_KEY the server does not hold."""
    otherKey = secrets.token_urlsafe(SECRET_KEY_BYTES)
    return EncodeCardRef(LISTED_CARD_NUM, settings.model_copy(update={"SECRET_KEY": otherKey}))


def PageKeyCardRef(settings: "Settings") -> str:
    """Return a CCLI page key, a card_ref token whose payload is longer than a card key."""
    return EncodeCardRef(PAGE_KEY_PAYLOAD, settings)


class OpenedCard(NamedTuple):
    """A CCUP entry that reads its card: keys, whether the card is a card_ref, the mask shown."""

    cardNum: str
    acctId: str
    sentAsCardRef: bool
    maskedCardNum: str


OPENED_CARDS = [
    OpenedCard(ZERO_CARD_NUM, LIST_ACCT_ID, True, "************0000"),
    OpenedCard(LISTED_CARD_NUM, ZERO_ACCT_ID, True, "************4321"),
    OpenedCard(LISTED_CARD_NUM, LIST_ACCT_ID, False, "************4321"),
]
OPENED_CARD_IDS = ["list-zero-card", "list-zero-account", "typed-keys"]


def ShownFields(openedCard: OpenedCard) -> CardScreenFields:
    """Return the COCRDUP fields 3200 shows for SHOW_DETAILS from CCUP-OLD-* (lines 1107-1112)."""
    return CardScreenFields(
        acct_id=openedCard.acctId,
        card_num=openedCard.maskedCardNum,
        embossed_name=UPPER_EMBOSSED_NAME,
        active_status=ACTIVE_STATUS,
        expiry_month=EXPIRY_MONTH,
        expiry_year=EXPIRY_YEAR,
    )


@pytest.mark.parametrize("openedCard", OPENED_CARDS, ids=OPENED_CARD_IDS)
async def test_entry_reads_card_into_show_details(cardRepository, testSettings, openedCard):
    """A list card_ref with its row account, or valid typed keys, read the card and show it."""
    cardKey = openedCard.cardNum
    if openedCard.sentAsCardRef:
        cardKey = EncodeCardRef(openedCard.cardNum, testSettings)
    context = BuildContext(testSettings)
    screen = await cocrdupc.FetchCardForEdit(context, cardKey, openedCard.acctId)
    assert screen.state == UpdateScreenState.SHOW_DETAILS
    assert (screen.message, screen.info_message) == (NO_MESSAGE, DETAILS_SHOWN_INFO)
    assert screen.fields == ShownFields(openedCard)
    assert (screen.field_errors, screen.cursor_field) == ([], NAME_FIELD)
    cardRepository.GetCard.assert_awaited_once_with(openedCard.cardNum)
    tokenState = cocrdupc.DecodeUpdateToken(screen.screen_token, testSettings)
    assert (tokenState.acctId, tokenState.cardNum) == (openedCard.acctId, openedCard.cardNum)


class RefusedKeys(NamedTuple):
    """Keys the 1210 and 1220 edits refuse, and the first literal and failing field they set."""

    cardNum: str
    acctText: str
    sentAsCardRef: bool
    expectedMessage: str
    expectedField: str


REFUSED_KEYS = [
    RefusedKeys(ZERO_CARD_NUM, LIST_ACCT_ID, False, CARD_NOT_PROVIDED_MESSAGE, CARD_NUM_FIELD),
    RefusedKeys(LISTED_CARD_NUM, NO_ACCT_TEXT, True, ACCOUNT_NOT_PROVIDED_MESSAGE, ACCT_ID_FIELD),
    RefusedKeys(LISTED_CARD_NUM, SHORT_ACCT_TEXT, True, ACCOUNT_FILTER_MESSAGE, ACCT_ID_FIELD),
    RefusedKeys(LISTED_CARD_NUM, NON_DIGIT_ACCT_TEXT, True, ACCOUNT_FILTER_MESSAGE, ACCT_ID_FIELD),
]
REFUSED_KEY_IDS = [
    "typed-zero-card",
    "card-ref-blank-account",
    "card-ref-short-account",
    "card-ref-non-digit-account",
]


@pytest.mark.parametrize("refusedKeys", REFUSED_KEYS, ids=REFUSED_KEY_IDS)
async def test_keys_not_from_list_run_key_edits(cardRepository, testSettings, refusedKeys):
    """Typed keys, or a card_ref with no row account, are edited and refused before any read."""
    cardKey = refusedKeys.cardNum
    if refusedKeys.sentAsCardRef:
        cardKey = EncodeCardRef(refusedKeys.cardNum, testSettings)
    context = BuildContext(testSettings)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdupc.FetchCardForEdit(context, cardKey, refusedKeys.acctText)
    body = raised.value.Body()
    assert raised.value.httpStatus == EDIT_FAILED_STATUS
    assert body["message"] == refusedKeys.expectedMessage
    assert body["field_errors"] == [refusedKeys.expectedField]
    assert body["cursor_field"] == refusedKeys.expectedField
    assert body["state"] == UpdateScreenState.DETAILS_NOT_FETCHED
    cardRepository.GetCard.assert_not_awaited()


@pytest.mark.parametrize(
    "cardRefBuilder", [ForgedCardRef, PageKeyCardRef], ids=["forged", "page-key"]
)
async def test_card_ref_not_carrying_card_key_is_not_found(
    cardRepository, testSettings, cardRefBuilder
):
    """A card_ref the server cannot open as a card key is NOTFND before any read or edit."""
    context = BuildContext(testSettings)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdupc.FetchCardForEdit(context, cardRefBuilder(testSettings), LIST_ACCT_ID)
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert raised.value.Body()["message"] == CARD_NOT_FOUND_MESSAGE
    assert raised.value.Body()["field_errors"] == [ACCT_ID_FIELD, CARD_NUM_FIELD]
    cardRepository.GetCard.assert_not_awaited()


@pytest.mark.parametrize("stateCode", FINISHED_STATE_CODES)
def test_finished_state_token_keeps_new_card_data(testSettings, stateCode):
    """A C, L or F token decodes with the CCUP-NEW-* values written or attempted."""
    beforeImage = cocrdupc.BeforeImage(
        cvv=CARD_CVV,
        name=UPPER_EMBOSSED_NAME,
        year=EXPIRY_YEAR,
        month=EXPIRY_MONTH,
        day=EXPIRY_DAY,
        status=ACTIVE_STATUS,
    )
    newData = cocrdupc.NewCardData(name=NEW_NAME, status=NEW_STATUS, month=NEW_MONTH, year=NEW_YEAR)
    state = cocrdupc.CardUpdateState(stateCode, LIST_ACCT_ID, ZERO_CARD_NUM, beforeImage, newData)
    token = cocrdupc.EncodeUpdateToken(state, testSettings)
    assert cocrdupc.DecodeUpdateToken(token, testSettings) == state
