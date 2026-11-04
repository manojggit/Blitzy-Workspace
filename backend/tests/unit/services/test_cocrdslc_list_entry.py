"""Unit tests for the COCRDSLC (CCDL) entry from the card list, against its typed-key ENTER.

cbl/COCRDSLC.cbl lines 337-348 (WHEN CDEMO-PGM-ENTER AND CDEMO-FROM-PROGRAM EQUAL
LIT-CCLISTPGM) SET INPUT-OK, move the account and card COCRDLIC passed from the selected row
(cbl/COCRDLIC.cbl lines 531-534) to CC-ACCT-ID-N and CC-CARD-NUM-N, PERFORM 9000-READ-DATA and
1000-SEND-MAP: no 2210-EDIT-ACCOUNT or 2220-EDIT-CARD runs. The all-zero refusal of
2220-EDIT-CARD (lines 685-720) belongs to keys typed on the COCRDSL screen. Expected values are
typed from those lines and the 88 levels they set:
- a list card_ref with its row account reads the card and shows it with ``   Displaying
    requested details`` (line 130), the all-zero card key and the all-zero account included;
- the all-zero card typed is ``Card number not provided`` (line 141);
- a card_ref with an account no list row carries runs 2210 (lines 139, 670);
- a forged card_ref, or a path declared a card_ref (``isCardRef``) that is no token, gives ``Did
    not find cards for this search condition`` (line 154); the same short text typed is
    ``CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER`` (line 711).

app.services.cards.cocrdslc's CardRepository is replaced by a stub whose GetCard is an
AsyncMock, so no database is needed.
"""

import secrets
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.core.security import DecodeCardRef, EncodeCardRef
from app.schemas.cards import CardDetailResponse
from app.services.cards import cocrdslc

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
# A cardRef query value no server issued, short enough to be CARDSID text when typed.
SHORT_GARBLED_TEXT = "garbage"
SECRET_KEY_BYTES = 48

# The CARDDAT record the stub reads (CVACT02Y), shown by 1200-SETUP-SCREEN-VARS (lines 474-486).
EMBOSSED_NAME = "Zero Key Holder"
CARD_CVV = "123"
EXPIRATION_DATE = date(2027, 3, 31)
EXPIRY_YEAR = "2027"
EXPIRY_MONTH = "03"
ACTIVE_STATUS = "Y"

# WS-INFO-MSG and WS-RETURN-MSG literals (lines 130-154, 670); the leading spaces are literal.
DETAILS_SHOWN_INFO = "   Displaying requested details"
PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number"
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
CARD_NOT_PROVIDED_MESSAGE = "Card number not provided"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
NO_MESSAGE = ""
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
EDIT_FAILED_STATUS = 400
NOT_FOUND_STATUS = 404


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
    """Make cocrdslc build the stub wherever it builds a CardRepository, and return the stub."""
    stubRepository = StubCardRepository()
    monkeypatch.setattr(cocrdslc, "CardRepository", stubRepository.ForContext)
    return stubRepository


def BuildContext(settings: "Settings") -> OnlineContext:
    """Return an online context holding only the settings the card_ref cipher uses."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=settings)


def ForgedCardRef(settings: "Settings") -> str:
    """Return a card_ref of a listed card sealed under a SECRET_KEY the server does not hold."""
    otherKey = secrets.token_urlsafe(SECRET_KEY_BYTES)
    return EncodeCardRef(LISTED_CARD_NUM, settings.model_copy(update={"SECRET_KEY": otherKey}))


class ListedCard(NamedTuple):
    """A COCRDLI row selected with S: its card key, its account and the mask CCDL shows."""

    cardNum: str
    acctId: str
    maskedCardNum: str


LISTED_CARDS = [
    pytest.param(ListedCard(ZERO_CARD_NUM, LIST_ACCT_ID, "************0000"), id="zero-card"),
    pytest.param(ListedCard(LISTED_CARD_NUM, LIST_ACCT_ID, "************4321"), id="card"),
    pytest.param(ListedCard(LISTED_CARD_NUM, ZERO_ACCT_ID, "************4321"), id="zero-account"),
]


def ShownDetail(listedCard: ListedCard, cardRef: str) -> CardDetailResponse:
    """Return the COCRDSL screen 1200-SETUP-SCREEN-VARS sends for FOUND-CARDS-FOR-ACCOUNT."""
    return CardDetailResponse(
        acct_id=listedCard.acctId,
        card_ref=cardRef,
        card_num_masked=listedCard.maskedCardNum,
        embossed_name=EMBOSSED_NAME,
        active_status=ACTIVE_STATUS,
        expiry_month=EXPIRY_MONTH,
        expiry_year=EXPIRY_YEAR,
        message=NO_MESSAGE,
        info_message=DETAILS_SHOWN_INFO,
        field_errors=[],
        cursor_field=ACCT_ID_FIELD,
    )


@pytest.mark.parametrize("isCardRef", [False, True], ids=["by-length", "declared"])
@pytest.mark.parametrize("listedCard", LISTED_CARDS)
async def test_list_entry_reads_card_without_key_edits(
    cardRepository, testSettings, listedCard, isCardRef
):
    """A list card_ref with its row account, declared or not, reads the card and shows it."""
    cardRef = EncodeCardRef(listedCard.cardNum, testSettings)
    context = BuildContext(testSettings)
    detail = await cocrdslc.ViewCard(context, cardRef, listedCard.acctId, isCardRef=isCardRef)
    assert DecodeCardRef(detail.card_ref, testSettings) == listedCard.cardNum
    assert detail == ShownDetail(listedCard, detail.card_ref)
    cardRepository.GetCard.assert_awaited_once_with(listedCard.cardNum)


class RefusedKeys(NamedTuple):
    """Keys 2210 and 2220 refuse, and the first literal and the failing field they set."""

    cardNum: str
    acctText: str
    sentAsCardRef: bool
    expectedMessage: str
    expectedField: str


REFUSED_KEYS = [
    RefusedKeys(ZERO_CARD_NUM, LIST_ACCT_ID, False, CARD_NOT_PROVIDED_MESSAGE, CARD_NUM_FIELD),
    RefusedKeys(SHORT_GARBLED_TEXT, LIST_ACCT_ID, False, CARD_FILTER_MESSAGE, CARD_NUM_FIELD),
    RefusedKeys(LISTED_CARD_NUM, NO_ACCT_TEXT, True, ACCOUNT_NOT_PROVIDED_MESSAGE, ACCT_ID_FIELD),
    RefusedKeys(LISTED_CARD_NUM, SHORT_ACCT_TEXT, True, ACCOUNT_FILTER_MESSAGE, ACCT_ID_FIELD),
    RefusedKeys(LISTED_CARD_NUM, NON_DIGIT_ACCT_TEXT, True, ACCOUNT_FILTER_MESSAGE, ACCT_ID_FIELD),
]
REFUSED_KEY_IDS = [
    "typed-zero-card",
    "typed-short-text",
    "card-ref-blank-account",
    "card-ref-short-account",
    "card-ref-non-digit-account",
]


def ShortGarbledCardKey(settings: "Settings") -> str:
    """Return a cardRef query value no server issued, as short as a typed card number."""
    return SHORT_GARBLED_TEXT


def CardNumberCardKey(settings: "Settings") -> str:
    """Return a 16-digit card number sent where a card_ref belongs."""
    return LISTED_CARD_NUM


class UnopenableCardRef(NamedTuple):
    """A ``{cardNum}`` path value no card_ref decode opens, and whether the query declares it."""

    cardKeyBuilder: Callable[["Settings"], str]
    isCardRef: bool


UNOPENABLE_CARD_REFS = [
    pytest.param(UnopenableCardRef(ForgedCardRef, False), id="forged"),
    pytest.param(UnopenableCardRef(ShortGarbledCardKey, True), id="declared-short-text"),
    pytest.param(UnopenableCardRef(CardNumberCardKey, True), id="declared-card-number"),
]


@pytest.mark.parametrize("refusedKeys", REFUSED_KEYS, ids=REFUSED_KEY_IDS)
async def test_keys_not_from_list_run_key_edits(cardRepository, testSettings, refusedKeys):
    """Typed keys, or a card_ref with no row account, are edited and refused before any read."""
    cardKey = refusedKeys.cardNum
    if refusedKeys.sentAsCardRef:
        cardKey = EncodeCardRef(refusedKeys.cardNum, testSettings)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(BuildContext(testSettings), cardKey, refusedKeys.acctText)
    body = raised.value.Body()
    assert raised.value.httpStatus == EDIT_FAILED_STATUS
    assert (body["message"], body["info_message"]) == (
        refusedKeys.expectedMessage,
        PROMPT_FOR_KEYS_INFO,
    )
    assert body["field_errors"] == [refusedKeys.expectedField]
    assert body["cursor_field"] == refusedKeys.expectedField
    cardRepository.GetCard.assert_not_awaited()


@pytest.mark.parametrize("unopenable", UNOPENABLE_CARD_REFS)
async def test_unopenable_card_ref_is_not_found(cardRepository, testSettings, unopenable):
    """A card_ref the server cannot open is NOTFND with both keys failing, before any read.

    A declared card_ref is decoded whatever its length, so short text and a card number sent
    as one are never taken for typed CARDSID text.
    """
    context = BuildContext(testSettings)
    cardKey = unopenable.cardKeyBuilder(testSettings)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(context, cardKey, LIST_ACCT_ID, isCardRef=unopenable.isCardRef)
    body = raised.value.Body()
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert (body["message"], body["info_message"]) == (CARD_NOT_FOUND_MESSAGE, PROMPT_FOR_KEYS_INFO)
    assert body["field_errors"] == [ACCT_ID_FIELD, CARD_NUM_FIELD]
    cardRepository.GetCard.assert_not_awaited()
