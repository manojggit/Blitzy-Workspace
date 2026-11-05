"""Unit tests for binding a COCRDUPC (CCUP) screen token to the card its PUT path names.

The ``screen_token`` replaces the COMMAREA extension WS-THIS-PROGCOMMAREA, which carried the
keys of the card on the COCRDUP screen; AAP 0.6.9 treats an invalid or expired token as
DETAILS_NOT_FETCHED, so a forged or expired token restarts the flow. A token issued for
another card than the ``PUT /api/v1/cards/{cardNum}`` path names is such an invalid token.
Expected values are typed from cbl/COCRDUPC.cbl and the 88 levels it sets:

- a SHOW_DETAILS, CHANGES_NOT_OK or CHANGES_OK_NOT_CONFIRMED token held for one card and sent
    with any key on another card's path, typed or as a ``card_ref``, runs the not-fetched ENTER
    (1210 and 1220 key edits, then 9000-READ-DATA, lines 645-661): the keyed card is shown in
    SHOW_DETAILS with ``Details of selected card shown above`` (line 161), and the token's own
    card is neither read nor locked nor rewritten;
- with blank keys on another card's path it gives ``No input received`` (line 187) on the key
    screen;
- on a path that is no card key (a forged or expired ``card_ref``, a CCLI page key, garbled
    text) it gives the 404 ``Did not find cards for this search condition`` (AAP 0.7.1) on the
    key screen with blank keys, whatever the key, and nothing is read;
- on the path of its own card, typed or as a ``card_ref``, the token holds: F5 rewrites it
    (9200-WRITE-PROCESSING, ``Changes committed to database``, line 171) and F12 re-reads it;
- a done or failed (C, L, F) token resets to the empty key screen whatever the path (lines
    517-528), reading nothing.

app.services.cards.cocrdupc's CardRepository is replaced by a stub of AsyncMocks and its
WriteUnit by a context manager yielding a stand-in connection, so no database is needed.
"""

import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from itertools import product
from types import SimpleNamespace
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import fernet
from cryptography.fernet import InvalidToken

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.core.security import SECONDS_PER_MINUTE, DecodeCardRef, EncodeCardRef
from app.schemas.cards import CardScreen, CardScreenFields, CardUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.cards import cocrdupc

if TYPE_CHECKING:
    from app.core.config import Settings

# The card the token holds and the other card whose path it is replayed on (CVACT02Y keys).
HELD_ACCT_ID = "00000000004"
HELD_CARD_NUM = "0000000000001111"
PATH_ACCT_ID = "00000000005"
PATH_CARD_NUM = "0000000000002222"
MASKED_PATH_CARD_NUM = "************2222"
# The CARDDAT records the stub reads, by card number: account and CARD-EMBOSSED-NAME.
CARD_RECORDS = {
    HELD_CARD_NUM: (HELD_ACCT_ID, "Held Card Holder"),
    PATH_CARD_NUM: (PATH_ACCT_ID, "Path Card Holder"),
}
HELD_UPPER_NAME = "HELD CARD HOLDER"
PATH_UPPER_NAME = "PATH CARD HOLDER"
CARD_CVV = "123"
EXPIRATION_DATE = date(2027, 3, 15)
EXPIRY_YEAR = "2027"
EXPIRY_MONTH = "03"
EXPIRY_DAY = "15"
ACTIVE_STATUS = "Y"
# CCUP-NEW-CARDDATA validated before F5, and the date its REWRITE stores (lines 1461-1475).
NEW_NAME = "Held Card Renamed"
NEW_STATUS = "N"
NEW_MONTH = "11"
NEW_YEAR = "2031"
NEW_EXPIRATION_DATE = date(2031, 11, 15)
# A CCLI page key's payload (both X(16) keys and two paging flags) and text that is no token.
PAGE_KEY_PAYLOAD = HELD_CARD_NUM + PATH_CARD_NUM + "NN"
GARBLED_CARD_KEY = "not-a-card-ref-" * 3
SECRET_KEY_BYTES = 48
# How long before ACCESS_TOKEN_EXPIRE_MINUTES ran out the expired card_ref was sealed, and the
# longer lifetime under which that card_ref still opens.
EXPIRY_MARGIN_SECONDS = 60
EXTENDED_LIFETIME_MINUTES = 10

# WS-RETURN-MSG and WS-INFO-MSG 88 levels (lines 157-187).
DETAILS_SHOWN_INFO = "Details of selected card shown above"
PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number"
UPDATE_SUCCESS_INFO = "Changes committed to database"
NO_INPUT_MESSAGE = "No input received"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
NAME_FIELD = "embossed_name"
EDIT_FAILED_STATUS = 400
NOT_FOUND_STATUS = 404
# The connection the stubbed WriteUnit yields to the repository's READ UPDATE and REWRITE.
WRITE_UNIT_CONNECTION = object()

# CCUP-SHOW-DETAILS, CCUP-CHANGES-NOT-OK and CCUP-CHANGES-OK-NOT-CONFIRMED: details fetched.
FETCHED_STATE_CODES = ["S", "E", "N"]
# CCUP-CHANGES-OKAYED-AND-DONE, COULD-NOT-LOCK-FOR-UPDATE and LOCKED-BUT-UPDATE-FAILED.
FINISHED_STATE_CODES = ["C", "L", "F"]
CONFIRM_STATE_CODE = "N"
# The states whose token carries no CCUP-NEW-* values.
DETAIL_STATE_CODES = ("S", "E")
EVERY_ACTION = [UpdateAction.ENTER, UpdateAction.SAVE, UpdateAction.CANCEL]

BEFORE_IMAGE = cocrdupc.BeforeImage(
    cvv=CARD_CVV,
    name=HELD_UPPER_NAME,
    year=EXPIRY_YEAR,
    month=EXPIRY_MONTH,
    day=EXPIRY_DAY,
    status=ACTIVE_STATUS,
)
NEW_DATA = cocrdupc.NewCardData(name=NEW_NAME, status=NEW_STATUS, month=NEW_MONTH, year=NEW_YEAR)


def CardRecord(cardNum: str) -> dict[str, object]:
    """Return the CARDDAT record of ``cardNum`` as CardRepository.GetCard maps it."""
    acctId, embossedName = CARD_RECORDS[cardNum]
    return {
        "card_num": cardNum,
        "acct_id": acctId,
        "cvv_cd": CARD_CVV,
        "embossed_name": embossedName,
        "expiration_date": EXPIRATION_DATE,
        "active_status": ACTIVE_STATUS,
    }


def LockedCardRecord(unit: object, cardNum: str) -> dict[str, object]:
    """Return the record CardRepository.GetCardForUpdate locks in ``unit``: unchanged."""
    return CardRecord(cardNum)


class StubCardRepository:
    """CardRepository's CARDDAT READ, READ UPDATE and REWRITE as AsyncMocks."""

    def __init__(self) -> None:
        self.GetCard = AsyncMock(side_effect=CardRecord)
        self.GetCardForUpdate = AsyncMock(side_effect=LockedCardRecord)
        self.UpdateCard = AsyncMock(return_value=None)

    def ForContext(self, context: OnlineContext) -> "StubCardRepository":
        """Return this stub as the repository the service builds from its context."""
        return self

    def AssertNothingWritten(self) -> None:
        """Assert that no READ UPDATE locked a card and no REWRITE ran."""
        self.GetCardForUpdate.assert_not_awaited()
        self.UpdateCard.assert_not_awaited()


@asynccontextmanager
async def StubWriteUnit(engine: object, fileName: str) -> AsyncIterator[object]:
    """Yield the stand-in connection of one CARDDAT write unit; nothing is committed."""
    yield WRITE_UNIT_CONNECTION


@pytest.fixture(name="cardRepository")
def CardRepositoryStub(monkeypatch) -> StubCardRepository:
    """Make cocrdupc build the stub repository and write unit, and return the stub."""
    stubRepository = StubCardRepository()
    monkeypatch.setattr(cocrdupc, "CardRepository", stubRepository.ForContext)
    monkeypatch.setattr(cocrdupc, "WriteUnit", StubWriteUnit)
    return stubRepository


def BuildContext(settings: "Settings") -> OnlineContext:
    """Return an online context with the settings the ciphers use and an unused engine."""
    # The request's own state is where core.locks.RequestEngine looks before app.state.engine.
    appState = SimpleNamespace(engine=None)
    request = SimpleNamespace(app=SimpleNamespace(state=appState), state=SimpleNamespace())
    return OnlineContext(request=request, session=SimpleNamespace(), settings=settings)


def HeldToken(stateCode: str, settings: "Settings") -> str:
    """Return the screen_token of the held card in ``stateCode``; N, C, L, F carry CCUP-NEW-*."""
    newData = None if stateCode in DETAIL_STATE_CODES else NEW_DATA
    state = cocrdupc.CardUpdateState(stateCode, HELD_ACCT_ID, HELD_CARD_NUM, BEFORE_IMAGE, newData)
    return cocrdupc.EncodeUpdateToken(state, settings)


def OtherCardTyped(settings: "Settings") -> str:
    """The other card's path as CARDSID was typed."""
    return PATH_CARD_NUM


def OtherCardRef(settings: "Settings") -> str:
    """The other card's path as the card_ref the card list issues."""
    return EncodeCardRef(PATH_CARD_NUM, settings)


def HeldCardTyped(settings: "Settings") -> str:
    """The held card's path as CARDSID was typed."""
    return HELD_CARD_NUM


def HeldCardRef(settings: "Settings") -> str:
    """The held card's path as the card_ref its last CardScreen carried."""
    return EncodeCardRef(HELD_CARD_NUM, settings)


def ForgedHeldCardRef(settings: "Settings") -> str:
    """The held card's card_ref sealed under a SECRET_KEY the server does not hold."""
    otherKey = secrets.token_urlsafe(SECRET_KEY_BYTES)
    return EncodeCardRef(HELD_CARD_NUM, settings.model_copy(update={"SECRET_KEY": otherKey}))


def ExpiredHeldCardRef(settings: "Settings") -> str:
    """The held card's card_ref, sealed longer ago than ACCESS_TOKEN_EXPIRE_MINUTES allows.

    Only the Fernet sealing clock is moved back, so the screen_token sent beside it is current.
    """
    lifetimeSeconds = settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    sealedAt = time.time() - lifetimeSeconds - EXPIRY_MARGIN_SECONDS
    sealingClock = SimpleNamespace(time=lambda: sealedAt)
    with patch.object(fernet, "time", sealingClock):
        return EncodeCardRef(HELD_CARD_NUM, settings)


def PageKeyCardRef(settings: "Settings") -> str:
    """A CCLI page key: a card_ref token whose payload, the held card first, is no card key."""
    return EncodeCardRef(PAGE_KEY_PAYLOAD, settings)


def GarbledCardKey(settings: "Settings") -> str:
    """Text longer than CARDSID that no cipher opens."""
    return GARBLED_CARD_KEY


type PathBuilder = Callable[["Settings"], str]
# Each path: its test id and the builder of the {cardNum} value the request is sent on.
OTHER_CARD_PATHS = [("other-card-typed", OtherCardTyped), ("other-card-ref", OtherCardRef)]
INVALID_CARD_REF_PATHS = [
    ("forged-card-ref", ForgedHeldCardRef),
    ("expired-card-ref", ExpiredHeldCardRef),
    ("page-key", PageKeyCardRef),
    ("garbled", GarbledCardKey),
]
UNMATCHED_PATHS = [*OTHER_CARD_PATHS, *INVALID_CARD_REF_PATHS]
HELD_CARD_PATHS = [("held-card-typed", HeldCardTyped), ("held-card-ref", HeldCardRef)]


class ReplayCase(NamedTuple):
    """A token state, the key pressed and the path the request is sent on."""

    stateCode: str
    action: UpdateAction
    pathBuilder: PathBuilder


def ReplayCases(
    stateCodes: list[str], actions: list[UpdateAction], paths: list[tuple[str, PathBuilder]]
) -> list:
    """Return every combination of state, key and path as one parametrised case each."""
    return [
        pytest.param(
            ReplayCase(stateCode, action, pathBuilder),
            id=f"{stateCode}-{action.value}-{pathId}",
        )
        for stateCode, action, (pathId, pathBuilder) in product(stateCodes, actions, paths)
    ]


async def SendKey(
    settings: "Settings", replayCase: ReplayCase, screenFields: CardScreenFields
) -> CardScreen:
    """Send the case's key with the held card's token on the case's path; return the screen."""
    updateRequest = CardUpdateRequest(
        action=replayCase.action,
        screen_token=HeldToken(replayCase.stateCode, settings),
        fields=screenFields,
    )
    cardKey = replayCase.pathBuilder(settings)
    return await cocrdupc.ProcessCardUpdate(BuildContext(settings), cardKey, updateRequest)


def PathCardShown() -> CardScreenFields:
    """Return the COCRDUP fields 3200 shows for the other card in SHOW_DETAILS."""
    return CardScreenFields(
        acct_id=PATH_ACCT_ID,
        card_num=MASKED_PATH_CARD_NUM,
        embossed_name=PATH_UPPER_NAME,
        active_status=ACTIVE_STATUS,
        expiry_month=EXPIRY_MONTH,
        expiry_year=EXPIRY_YEAR,
    )


@pytest.mark.parametrize(
    "replayCase", ReplayCases(FETCHED_STATE_CODES, EVERY_ACTION, OTHER_CARD_PATHS)
)
async def test_fetched_token_on_other_card_path_fetches_keyed_card(
    cardRepository, testSettings, replayCase
):
    """The not-fetched ENTER reads the keyed card; the held card is never read or rewritten."""
    keyedFields = CardScreenFields(acct_id=PATH_ACCT_ID, card_num=PATH_CARD_NUM)
    screen = await SendKey(testSettings, replayCase, keyedFields)
    assert screen.state == UpdateScreenState.SHOW_DETAILS
    assert (screen.message, screen.info_message) == ("", DETAILS_SHOWN_INFO)
    assert screen.fields == PathCardShown()
    cardRepository.GetCard.assert_awaited_once_with(PATH_CARD_NUM)
    cardRepository.AssertNothingWritten()
    tokenState = cocrdupc.DecodeUpdateToken(screen.screen_token, testSettings)
    assert (tokenState.acctId, tokenState.cardNum) == (PATH_ACCT_ID, PATH_CARD_NUM)


@pytest.mark.parametrize(
    "replayCase", ReplayCases(FETCHED_STATE_CODES, [UpdateAction.SAVE], OTHER_CARD_PATHS)
)
async def test_fetched_token_on_other_card_path_restarts_at_key_edits(
    cardRepository, testSettings, replayCase
):
    """With blank keys the restarted flow fails the key edits; nothing is read or written."""
    with pytest.raises(LegacyMessageError) as raised:
        await SendKey(testSettings, replayCase, CardScreenFields())
    body = raised.value.Body()
    assert raised.value.httpStatus == EDIT_FAILED_STATUS
    assert (body["message"], body["info_message"]) == (NO_INPUT_MESSAGE, PROMPT_FOR_KEYS_INFO)
    assert body["field_errors"] == [ACCT_ID_FIELD, CARD_NUM_FIELD]
    assert body["state"] == UpdateScreenState.DETAILS_NOT_FETCHED
    tokenState = cocrdupc.DecodeUpdateToken(body["screen_token"], testSettings)
    assert tokenState == cocrdupc.NOT_FETCHED_STATE
    cardRepository.GetCard.assert_not_awaited()
    cardRepository.AssertNothingWritten()


def test_expired_card_ref_carries_the_held_card(testSettings):
    """The expired path is the held card's own card_ref, refused for its age alone."""
    expiredCardRef = ExpiredHeldCardRef(testSettings)
    extendedLifetime = testSettings.ACCESS_TOKEN_EXPIRE_MINUTES + EXTENDED_LIFETIME_MINUTES
    extendedSettings = testSettings.model_copy(
        update={"ACCESS_TOKEN_EXPIRE_MINUTES": extendedLifetime}
    )
    assert DecodeCardRef(expiredCardRef, extendedSettings) == HELD_CARD_NUM
    with pytest.raises(InvalidToken):
        DecodeCardRef(expiredCardRef, testSettings)


@pytest.mark.parametrize(
    "replayCase", ReplayCases(FETCHED_STATE_CODES, EVERY_ACTION, INVALID_CARD_REF_PATHS)
)
async def test_fetched_token_on_invalid_card_ref_path_is_card_not_found(
    cardRepository, testSettings, replayCase
):
    """A path that is no card key gives the 404 key screen; the keyed card is not read either."""
    keyedFields = CardScreenFields(acct_id=PATH_ACCT_ID, card_num=PATH_CARD_NUM)
    with pytest.raises(LegacyMessageError) as raised:
        await SendKey(testSettings, replayCase, keyedFields)
    body = raised.value.Body()
    assert raised.value.httpStatus == NOT_FOUND_STATUS
    assert (body["message"], body["info_message"]) == (CARD_NOT_FOUND_MESSAGE, PROMPT_FOR_KEYS_INFO)
    assert body["field_errors"] == [ACCT_ID_FIELD, CARD_NUM_FIELD]
    assert body["cursor_field"] == ACCT_ID_FIELD
    assert body["state"] == UpdateScreenState.DETAILS_NOT_FETCHED
    assert body["fields"] == CardScreenFields().model_dump(mode="json")
    tokenState = cocrdupc.DecodeUpdateToken(body["screen_token"], testSettings)
    assert tokenState == cocrdupc.NOT_FETCHED_STATE
    cardRepository.GetCard.assert_not_awaited()
    cardRepository.AssertNothingWritten()


@pytest.mark.parametrize(
    "replayCase", ReplayCases([CONFIRM_STATE_CODE], [UpdateAction.SAVE], HELD_CARD_PATHS)
)
async def test_confirm_token_on_its_card_path_rewrites_its_card(
    cardRepository, testSettings, replayCase
):
    """F5 on the held card's own path keeps the token and rewrites its validated values."""
    screen = await SendKey(testSettings, replayCase, CardScreenFields())
    assert screen.state == UpdateScreenState.CHANGES_DONE
    assert screen.info_message == UPDATE_SUCCESS_INFO
    cardRepository.GetCard.assert_not_awaited()
    cardRepository.GetCardForUpdate.assert_awaited_once_with(WRITE_UNIT_CONNECTION, HELD_CARD_NUM)
    rewriteValues = {
        "card_num": HELD_CARD_NUM,
        "acct_id": HELD_ACCT_ID,
        "embossed_name": NEW_NAME,
        "expiration_date": NEW_EXPIRATION_DATE,
        "active_status": NEW_STATUS,
    }
    cardRepository.UpdateCard.assert_awaited_once_with(WRITE_UNIT_CONNECTION, rewriteValues)


@pytest.mark.parametrize(
    "replayCase", ReplayCases(FETCHED_STATE_CODES, [UpdateAction.CANCEL], HELD_CARD_PATHS)
)
async def test_fetched_token_on_its_card_path_rereads_its_card(
    cardRepository, testSettings, replayCase
):
    """F12 on the held card's own path keeps the token keys and re-reads the held card."""
    screen = await SendKey(testSettings, replayCase, CardScreenFields())
    assert (screen.state, screen.info_message) == (
        UpdateScreenState.SHOW_DETAILS,
        DETAILS_SHOWN_INFO,
    )
    assert (screen.fields.acct_id, screen.fields.embossed_name) == (HELD_ACCT_ID, HELD_UPPER_NAME)
    assert screen.cursor_field == NAME_FIELD
    cardRepository.GetCard.assert_awaited_once_with(HELD_CARD_NUM)
    cardRepository.AssertNothingWritten()


@pytest.mark.parametrize(
    "replayCase", ReplayCases(FINISHED_STATE_CODES, EVERY_ACTION, UNMATCHED_PATHS)
)
async def test_finished_token_on_other_path_resets_without_reading(
    cardRepository, testSettings, replayCase
):
    """A done or failed token resets to the empty key screen on any path, reading nothing."""
    keyedFields = CardScreenFields(acct_id=PATH_ACCT_ID, card_num=PATH_CARD_NUM)
    screen = await SendKey(testSettings, replayCase, keyedFields)
    assert screen.state == UpdateScreenState.DETAILS_NOT_FETCHED
    assert (screen.message, screen.info_message) == ("", PROMPT_FOR_KEYS_INFO)
    assert screen.fields == CardScreenFields(acct_id="", card_num="")
    assert screen.card_ref == ""
    cardRepository.GetCard.assert_not_awaited()
    cardRepository.AssertNothingWritten()
