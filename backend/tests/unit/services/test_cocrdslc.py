"""Unit tests for COCRDSLC (transaction CCDL, map COCRDSL): the card detail view.

app.services.cards.cocrdslc.ViewCard is called directly, with an online context of a regular
user, against the migrated and seeded test database. Expected values are typed from
cbl/COCRDSLC.cbl:

- 2200-EDIT-MAP-INPUTS (lines 608-641) turns a key of ``*`` or spaces into LOW-VALUES and runs
    2210-EDIT-ACCOUNT (lines 647-679) and 2220-EDIT-CARD (lines 685-720). Each edit sets its
    literal only while WS-RETURN-MSG-OFF holds and keeps its own flag, so the first literal is
    shown and every failing key is marked; two blank keys give ``No input received``. The
    cursor is the first failing key (1300-SETUP-SCREEN-ATTRS, lines 515-524).
- 9100-GETCARD-BYACCTCARD (lines 736-773) reads CARDDAT by card number only: NOTFND flags both
    keys with ``Did not find cards for this search condition``, WHEN OTHER flags the account
    with the WS-FILE-ERROR-MESSAGE text (lines 102-121).
- 1200-SETUP-SCREEN-VARS (lines 457-497) shows the account as entered (CC-ACCT-ID) and the
    card's name, expiry and status with ``   Displaying requested details``; any other outcome
    shows the prompt ``Please enter Account and Card Number``.

The card number leaves the service only masked and sealed in its card_ref, and no CVV leaves
it at all. CCDL never writes, so every case also checks that cards.cards is unchanged.
"""

import json
import re
import time
from datetime import date
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import Engine, text

from app.core.errors import FileIoError, LegacyMessageError
from app.core.security import CARD_REF_HKDF_INFO, DecodeCardRef, DeriveFernet, EncodeCardRef
from app.repositories.card_repository import CardRepository
from app.schemas.cards import CardDetailResponse
from app.services.cards import cocrdslc

if TYPE_CHECKING:
    # Annotation-only: the parameter types ViewCard and EncodeCardRef declare.
    from app.core.config import Settings
    from app.core.dependencies import OnlineContext

REGULAR_USER_TYPE = "U"

# WS-RETURN-MSG literals set on a reachable path (lines 134-158, 670, 711).
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
CARD_NOT_PROVIDED_MESSAGE = "Card number not provided"
NO_INPUT_MESSAGE = "No input received"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
NO_MESSAGE = ""
# WS-INFO-MSG 88 levels (lines 126-132); the three leading spaces are part of the literal.
DISPLAYING_INFO = "   Displaying requested details"
PROMPT_INFO = "Please enter Account and Card Number"

# The screen fields ACCTSID and CARDSID, as field_errors and cursor_field name them.
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
BOTH_KEY_FIELDS = [ACCT_ID_FIELD, CARD_NUM_FIELD]
EDIT_FAILED_STATUS = 400
NOT_FOUND_STATUS = 404
FILE_ERROR_STATUS = 503

# The seed account and its one card (data/EBCDIC CARDDATA), another seeded account, and a
# 16-digit card number no CARDDAT record has.
SEED_ACCT_ID = "00000000004"
SEED_CARD_NUM = "2988091353094312"
OTHER_ACCT_ID = "00000000001"
UNKNOWN_CARD_NUM = "9999999999999999"
# Keys the edits refuse: a 3-character account and a 4-character card (IS NOT NUMERIC on the
# space-padded field), and the '*' the map shows for a blank key.
NON_NUMERIC_ACCT_TEXT = "abc"
NON_NUMERIC_CARD_TEXT = "12ab"
BLANK_KEY_MARKER = "*"

# 9100-GETCARD-BYACCTCARD WHEN OTHER: 'READ' to ERROR-OPNAME, LIT-CARDFILENAME to ERROR-FILE.
READ_OPNAME = "READ"
CARD_FILE = "CARDDAT"
ERROR_OPNAME_WIDTH = 8
ERROR_FILE_WIDTH = 9
RETURN_MSG_LENGTH = 75

# CARD-NUM shown as twelve asterisks and its last four digits; a 16-digit run would be a PAN.
MASKED_PREFIX = "*" * 12
SHOWN_DIGIT_COUNT = 4
PAN_PATTERN = re.compile(r"\d{16}")
CVV_NAME_PART = "cvv"
SECONDS_PER_MINUTE = 60
EXPIRY_MARGIN_SECONDS = 60

SEED_CARD_QUERY = text(
    "SELECT acct_id, card_num, expiration_date, embossed_name, active_status"
    " FROM cards.cards WHERE acct_id = :acctId"
)
CARDS_IMAGE_QUERY = text("SELECT * FROM cards.cards ORDER BY card_num")


class SeededCard(NamedTuple):
    """The committed CARDDAT record (CVACT02Y) of the seed account."""

    acctId: str
    cardNum: str
    expiry: date | None
    name: str
    status: str


def CardsImage(syncEngine: Engine) -> tuple[dict[str, object], ...]:
    """Return every committed CARDDAT record, in card-number order."""
    with syncEngine.connect() as connection:
        return tuple(dict(record) for record in connection.execute(CARDS_IMAGE_QUERY).mappings())


class CcdlHarness(NamedTuple):
    """One CCDL request: its context, the settings sealing card_refs, and CARDDAT before it."""

    context: "OnlineContext"
    settings: "Settings"
    syncEngine: Engine
    cardsBefore: tuple[dict[str, object], ...]

    def AssertNothingWritten(self) -> None:
        """Fail unless every CARDDAT record is still as committed before the request."""
        assert CardsImage(self.syncEngine) == self.cardsBefore


@pytest.fixture(name="seedCard")
def SeedCard(migratedDatabase, syncEngine: Engine) -> SeededCard:
    """Return the one card of account 00000000004, read through syncEngine."""
    with syncEngine.connect() as connection:
        record = connection.execute(SEED_CARD_QUERY, {"acctId": SEED_ACCT_ID}).one()
    return SeededCard(*record)


@pytest_asyncio.fixture(name="ccdl")
async def CcdlHarnessFixture(makeOnlineContext, testSettings, syncEngine: Engine) -> CcdlHarness:
    """Return a regular user's CCDL context and the CARDDAT image committed before the test."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    return CcdlHarness(context, testSettings, syncEngine, CardsImage(syncEngine))


def RefusalBody(message: str, fieldErrors: list[str]) -> dict[str, object]:
    """Return a CCDL refusal body: the literal, the prompt, and the cursor on the first key."""
    return {
        "message": message,
        "info_message": PROMPT_INFO,
        "field_errors": fieldErrors,
        "cursor_field": fieldErrors[0],
    }


def ExpectedFileErrorMessage(error: FileIoError) -> str:
    """Return WS-FILE-ERROR-MESSAGE (lines 102-121) for a READ ending in ``error``.

    ERROR-OPNAME X(8) and ERROR-FILE X(9) are space-padded. WS-RESP-CD and WS-REAS-CD, both
    S9(09) COMP, moved to the X(10) ERROR-RESP and ERROR-RESP2 give nine digits and a space.
    The 80-byte group is cut to WS-RETURN-MSG X(75), whose trailing spaces are not shown.
    """
    fileErrorGroup = (
        "File Error: "
        + READ_OPNAME.ljust(ERROR_OPNAME_WIDTH)
        + " on "
        + error.fileName.ljust(ERROR_FILE_WIDTH)
        + " returned RESP "
        + f"{error.resp:09d} "
        + ",RESP2 "
        + f"{error.resp2:09d} "
        + " " * 5
    )
    return fileErrorGroup[:RETURN_MSG_LENGTH].rstrip(" ")


def ExpiryYearAndMonth(expiry: date | None) -> tuple[str, str]:
    """Return CARD-EXPIRY-YEAR (1:4) and CARD-EXPIRY-MONTH (6:2) of the YYYY-MM-DD expiry."""
    expiryText = expiry.isoformat() if expiry is not None else ""
    return (expiryText[0:4], expiryText[5:7])


def AssertShowsCard(detail: CardDetailResponse, card: SeededCard, settings: "Settings") -> None:
    """Fail unless ``detail`` is the FOUND-CARDS-FOR-ACCOUNT screen of ``card``."""
    assert (detail.message, detail.info_message) == (NO_MESSAGE, DISPLAYING_INFO)
    assert (detail.field_errors, detail.cursor_field) == ([], ACCT_ID_FIELD)
    assert detail.card_num_masked == MASKED_PREFIX + card.cardNum[-SHOWN_DIGIT_COUNT:]
    assert DecodeCardRef(detail.card_ref, settings) == card.cardNum
    assert (detail.embossed_name, detail.active_status) == (card.name, card.status)
    assert (detail.expiry_year, detail.expiry_month) == ExpiryYearAndMonth(card.expiry)


def AssertNoCardNumberOrCvv(detail: CardDetailResponse) -> None:
    """Fail when the response names a CVV field or holds a run of 16 digits, a whole PAN."""
    shownFields = detail.model_dump(mode="json")
    assert [fieldName for fieldName in shownFields if CVV_NAME_PART in fieldName] == []
    assert PAN_PATTERN.search(json.dumps(shownFields)) is None


def CardRefCipher(settings: "Settings") -> Fernet:
    """Return the Fernet cipher that seals card_refs under the server's SECRET_KEY."""
    return DeriveFernet(settings.SECRET_KEY, CARD_REF_HKDF_INFO)


def TypedCardKey(cardNum: str, settings: "Settings") -> str:
    """Return the card number as keyed in CARDSID; ``settings`` keeps the builders' signature."""
    return cardNum


def SealedCardKey(cardNum: str, settings: "Settings") -> str:
    """Return the card number sealed in a card_ref, as a card-list row carries it."""
    return EncodeCardRef(cardNum, settings)


def TamperedCardRef(cardNum: str, settings: "Settings") -> str:
    """Return a card_ref of ``cardNum`` with its middle character changed."""
    cardRef = EncodeCardRef(cardNum, settings)
    middle = len(cardRef) // 2
    replacement = "B" if cardRef[middle] == "A" else "A"
    return cardRef[:middle] + replacement + cardRef[middle + 1 :]


def ExpiredCardRef(cardNum: str, settings: "Settings") -> str:
    """Return a card_ref of ``cardNum`` sealed a minute more than one lifetime ago."""
    lifetimeSeconds = settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    sealedAt = int(time.time()) - lifetimeSeconds - EXPIRY_MARGIN_SECONDS
    sealedToken = CardRefCipher(settings).encrypt_at_time(cardNum.encode("utf-8"), sealedAt)
    return sealedToken.decode("ascii")


class KeyCase(NamedTuple):
    """Keys typed on COCRDSL, and the literal and failing keys 2200-EDIT-MAP-INPUTS sets."""

    acctText: str
    cardText: str
    message: str
    fieldErrors: list[str]


KEY_CASES = [
    pytest.param(
        KeyCase("", SEED_CARD_NUM, ACCOUNT_NOT_PROVIDED_MESSAGE, [ACCT_ID_FIELD]),
        id="account-blank",
    ),
    pytest.param(
        KeyCase(SEED_ACCT_ID, "", CARD_NOT_PROVIDED_MESSAGE, [CARD_NUM_FIELD]),
        id="card-blank",
    ),
    pytest.param(KeyCase("", "", NO_INPUT_MESSAGE, BOTH_KEY_FIELDS), id="both-blank"),
    pytest.param(
        KeyCase(BLANK_KEY_MARKER, BLANK_KEY_MARKER, NO_INPUT_MESSAGE, BOTH_KEY_FIELDS),
        id="both-asterisk",
    ),
    pytest.param(
        KeyCase(NON_NUMERIC_ACCT_TEXT, SEED_CARD_NUM, ACCOUNT_FILTER_MESSAGE, [ACCT_ID_FIELD]),
        id="account-not-numeric",
    ),
    pytest.param(
        KeyCase(SEED_ACCT_ID, NON_NUMERIC_CARD_TEXT, CARD_FILTER_MESSAGE, [CARD_NUM_FIELD]),
        id="card-not-numeric",
    ),
    pytest.param(
        KeyCase(
            NON_NUMERIC_ACCT_TEXT, NON_NUMERIC_CARD_TEXT, ACCOUNT_FILTER_MESSAGE, BOTH_KEY_FIELDS
        ),
        id="both-not-numeric",
    ),
]
CARD_KEY_BUILDERS = [
    pytest.param(TypedCardKey, id="typed-card-number"),
    pytest.param(SealedCardKey, id="card-ref"),
]
REFUSED_CARD_REFS = [
    pytest.param(TamperedCardRef, id="tampered-card-ref"),
    pytest.param(ExpiredCardRef, id="expired-card-ref"),
]


@pytest.mark.parametrize("keyCase", KEY_CASES)
async def test_key_edits_show_first_literal_and_every_failing_key(ccdl, keyCase):
    """Both key edits run; the first literal set is kept and each failing key is marked."""
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(ccdl.context, keyCase.cardText, keyCase.acctText)
    assert raised.value.httpStatus == EDIT_FAILED_STATUS
    assert raised.value.Body() == RefusalBody(keyCase.message, keyCase.fieldErrors)
    ccdl.AssertNothingWritten()


async def test_unknown_card_is_not_found(ccdl):
    """A valid account and a 16-digit card no CARDDAT record has give NOTFND on both keys."""
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(ccdl.context, UNKNOWN_CARD_NUM, SEED_ACCT_ID)
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert raised.value.Body() == RefusalBody(CARD_NOT_FOUND_MESSAGE, BOTH_KEY_FIELDS)
    ccdl.AssertNothingWritten()


async def test_card_is_read_by_card_number_only(ccdl, seedCard):
    """Another valid account passes 2210 and is never compared with the card's own account."""
    detail = await cocrdslc.ViewCard(ccdl.context, seedCard.cardNum, OTHER_ACCT_ID)
    AssertShowsCard(detail, seedCard, ccdl.settings)
    # 1200-SETUP-SCREEN-VARS moves CC-ACCT-ID, the account as entered, to ACCTSIDO; the card's
    # own CARD-ACCT-ID is never shown, so the screen keeps the account the user typed.
    assert (detail.acct_id, seedCard.acctId) == (OTHER_ACCT_ID, SEED_ACCT_ID)
    ccdl.AssertNothingWritten()


@pytest.mark.parametrize("buildCardKey", CARD_KEY_BUILDERS)
async def test_card_is_shown_masked_without_cvv(ccdl, seedCard, buildCardKey):
    assert (seedCard.acctId, seedCard.cardNum) == (SEED_ACCT_ID, SEED_CARD_NUM)
    cardKey = buildCardKey(seedCard.cardNum, ccdl.settings)
    detail = await cocrdslc.ViewCard(ccdl.context, cardKey, seedCard.acctId)
    AssertShowsCard(detail, seedCard, ccdl.settings)
    assert detail.acct_id == seedCard.acctId
    AssertNoCardNumberOrCvv(detail)
    ccdl.AssertNothingWritten()


@pytest.mark.parametrize("forgeCardRef", REFUSED_CARD_REFS)
async def test_refused_card_ref_is_not_found(ccdl, seedCard, forgeCardRef):
    """A card_ref the server cannot open, tampered or expired, is NOTFND on both keys."""
    cardRef = forgeCardRef(seedCard.cardNum, ccdl.settings)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(ccdl.context, cardRef, seedCard.acctId)
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert raised.value.Body() == RefusalBody(CARD_NOT_FOUND_MESSAGE, BOTH_KEY_FIELDS)
    ccdl.AssertNothingWritten()


def test_expired_card_ref_is_sound_apart_from_its_age(testSettings):
    """The expired card_ref opens under the server key when its age is not checked."""
    expiredCardRef = ExpiredCardRef(SEED_CARD_NUM, testSettings)
    openedCardNum = CardRefCipher(testSettings).decrypt(expiredCardRef).decode("utf-8")
    assert openedCardNum == SEED_CARD_NUM


async def test_card_file_error_shows_the_read_error_text(ccdl, monkeypatch):
    """A CARDDAT READ ending WHEN OTHER shows WS-FILE-ERROR-MESSAGE with the account flagged."""
    ioError = FileIoError(CARD_FILE)
    getCard = AsyncMock(side_effect=ioError)
    monkeypatch.setattr(CardRepository, "GetCard", getCard)
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdslc.ViewCard(ccdl.context, SEED_CARD_NUM, SEED_ACCT_ID)
    assert raised.value.httpStatus == FILE_ERROR_STATUS
    assert raised.value.Body() == RefusalBody(ExpectedFileErrorMessage(ioError), [ACCT_ID_FIELD])
    getCard.assert_awaited_once_with(SEED_CARD_NUM)
    ccdl.AssertNothingWritten()
