"""Unit tests of app.services.cards.cocrdupc: COCRDUPC (transaction CCUP, BMS map COCRDUP).

The service is called directly, never over HTTP, against the migrated and seeded test database:
``FetchCardForEdit`` opens a card for update and ``ProcessCardUpdate`` answers one ENTER, F5
(save) or F12 (cancel). Expected values are transcribed from cbl/COCRDUPC.cbl:

- the WS-INFO-MSG and WS-RETURN-MSG 88 levels (lines 157-210) and the two filter literals of
  1210-EDIT-ACCOUNT and 1220-EDIT-CARD (lines 745, 789);
- 88 VALID-MONTH VALUES 1 THRU 12 and 88 VALID-YEAR VALUES 1950 THRU 2099 (lines 95, 99);
- the CCUP-CHANGE-ACTION state machine of 0000-MAIN and 2000-DECIDE-ACTION (lines 517-1022),
  where F5 is valid only once changes are validated, F12 only once details are fetched, and
  the no-change compare (lines 680-692) runs before every detail edit;
- 9100-GETCARD-BYACCTCARD WHEN OTHER (lines 1402-1411), which flags the account and shows the
  READ/CARDDAT WS-FILE-ERROR-MESSAGE (lines 133-152): a 503 on the key screen, whether the
  read is the fetch or the F12 re-read;
- 9200-WRITE-PROCESSING and 9300-CHECK-CHANGE-IN-REC (lines 1420-1511), which rewrite the
  name as entered, the status and the new year and month with the before-image day, and keep
  the CVV.

Every refusal is a LegacyMessageError whose body carries the four legacy keys and the screen's
``fields``, ``state`` and ``screen_token``; CCUP error bodies carry no ``card_ref``. Whether a
write happened is read back from cards.cards with plain SQL, and every test restores the seed.
"""

import secrets
import time
from collections.abc import Mapping
from datetime import date
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    HTTP_RECORD_CHANGED,
    HTTP_RECORD_LOCKED,
    FileIoError,
    LegacyMessageError,
    RecordLockError,
)
from app.core.security import (
    SCREEN_STATE_HKDF_INFO,
    SECONDS_PER_MINUTE,
    DecodeCardRef,
    DeriveFernet,
)
from app.repositories.card_repository import CARD_FILE_NAME, CardRepository
from app.schemas.cards import CardScreen, CardScreenFields, CardUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.cards import cocrdupc

# --- Seed card (data/EBCDIC/AWS.M2.CARDDEMO.CARDDATA.PS) and keys ------------------------------
SEED_CARD_NUM = "2988091353094312"
SEED_ACCT_ID = "00000000004"
MASKED_SEED_CARD_NUM = "************4312"
MISSING_CARD_NUM = "1111111111111111"
NON_NUMERIC_ACCT_TEXT = "0000000000A"
NON_NUMERIC_CARD_TEXT = "29880913530943XX"
BLANK_KEY_MARKER = "*"
REGULAR_USER_TYPE = "U"

# --- WS-RETURN-MSG 88 levels and filter literals (lines 177-210, 745, 789) ----------------------
ACCOUNT_NOT_PROVIDED_MESSAGE = "Account number not provided"
CARD_NOT_PROVIDED_MESSAGE = "Card number not provided"
NO_INPUT_RECEIVED_MESSAGE = "No input received"
ACCOUNT_FILTER_MESSAGE = "ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER"
CARD_FILTER_MESSAGE = "CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER"
CARD_NOT_FOUND_MESSAGE = "Did not find cards for this search condition"
CARD_NAME_NOT_PROVIDED_MESSAGE = "Card name not provided"
CARD_NAME_ALPHA_MESSAGE = "Card name can only contain alphabets and spaces"
CARD_STATUS_MESSAGE = "Card Active Status must be Y or N"
EXPIRY_MONTH_MESSAGE = "Card expiry month must be between 1 and 12"
EXPIRY_YEAR_MESSAGE = "Invalid card expiry year"
NO_CHANGES_MESSAGE = "No change detected with respect to values fetched."
LOCK_FAILED_MESSAGE = "Could not lock record for update"
RECORD_CHANGED_MESSAGE = "Record changed by some one else. Please review"
UPDATE_FAILED_MESSAGE = "Update of record failed"
NO_MESSAGE = ""
# 9100 WHEN OTHER (lines 1402-1411) fills WS-FILE-ERROR-MESSAGE (lines 133-152): 'READ' in
# ERROR-OPNAME X(8), LIT-CARDFILENAME in ERROR-FILE X(9), and IOERR's RESP 17 and RESP2 0 in
# ERROR-RESP and ERROR-RESP2 X(10), nine digits and a space each. The 80-byte group is cut to
# WS-RETURN-MSG X(75), whose trailing space is not shown.
READ_FILE_ERROR_MESSAGE = (
    "File Error: READ     on CARDDAT   returned RESP 000000017 ,RESP2 000000000"
)

# --- WS-INFO-MSG 88 levels (lines 157-171) -------------------------------------------------------
DETAILS_SHOWN_INFO = "Details of selected card shown above"
PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number"
PROMPT_FOR_CHANGES_INFO = "Update card details presented above."
PROMPT_FOR_CONFIRMATION_INFO = "Changes validated.Press F5 to save"
UPDATE_SUCCESS_INFO = "Changes committed to database"
UPDATE_FAILURE_INFO = "Changes unsuccessful. Please try again"

# --- COCRDUP fields (ACCTSID, CARDSID, CRDNAME, CRDSTCD, EXPMON, EXPYEAR) ------------------------
ACCT_ID_FIELD = "acct_id"
CARD_NUM_FIELD = "card_num"
NAME_FIELD = "embossed_name"
STATUS_FIELD = "active_status"
MONTH_FIELD = "expiry_month"
YEAR_FIELD = "expiry_year"
BOTH_KEY_FIELDS = (ACCT_ID_FIELD, CARD_NUM_FIELD)
# Every CCUP error body: the four legacy keys and the screen beside them.
CCUP_ERROR_BODY_KEYS = frozenset(
    {"message", "info_message", "field_errors", "cursor_field", "fields", "state", "screen_token"}
)

# --- Detail values keyed on the screen ---------------------------------------------------------
NEW_NAME = "Renamed Card Holder"
NEW_STATUS = "N"
NEW_MONTH = "02"
NEW_YEAR = "2031"
CONCURRENT_NAME = "Changed Elsewhere"
# One value per detail field that 1230-1260 refuse, keyed together.
INVALID_DETAIL_VALUES = {
    NAME_FIELD: "JOHN 3RD",
    STATUS_FIELD: "X",
    MONTH_FIELD: "13",
    YEAR_FIELD: "1949",
}
# Valid values unlike the validated ones, sent with F5 to prove they are not read.
OTHER_DETAIL_VALUES = {NAME_FIELD: "Ignored Name", STATUS_FIELD: "Y", MONTH_FIELD: "07"}
VALID_BOUNDARY_YEARS = ["1950", "2099"]

# --- Screen tokens -----------------------------------------------------------------------------
OTHER_SECRET_KEY_BYTES = 48
# How far past the token lifetime the expired token was issued.
EXPIRY_MARGIN_SECONDS = 60

CARD_ROW_QUERY = text(
    "SELECT acct_id, cvv_cd, embossed_name, expiration_date, active_status"
    " FROM cards.cards WHERE card_num = :cardNum"
)
RENAME_CARD_SQL = text("UPDATE cards.cards SET embossed_name = :name WHERE card_num = :cardNum")


class CardRow(NamedTuple):
    """The cards.cards columns a CCUP rewrite may touch, read with plain SQL."""

    acctId: str
    cvv: str
    name: str
    expiry: date | None
    status: str


class CardScenario(NamedTuple):
    """One regular user's CCUP context, the test database engine and the seed card's row.

    ``context`` is the OnlineContext the conftest makeOnlineContext fixture builds.
    """

    context: Any
    syncEngine: Engine
    seedRow: CardRow


class ExpectedScreen(NamedTuple):
    """A CardScreen answer: state, message, information line and cursor; no field fails."""

    state: UpdateScreenState
    message: str
    info: str
    cursorField: str


class ExpectedRefusal(NamedTuple):
    """A LegacyMessageError answer: status, both message lines, failing fields and state."""

    httpStatus: int
    message: str
    info: str
    fieldErrors: tuple[str, ...]
    cursorField: str
    state: UpdateScreenState


SHOWN_SCREEN = ExpectedScreen(
    UpdateScreenState.SHOW_DETAILS, NO_MESSAGE, DETAILS_SHOWN_INFO, NAME_FIELD
)
CONFIRM_SCREEN = ExpectedScreen(
    UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
    NO_MESSAGE,
    PROMPT_FOR_CONFIRMATION_INFO,
    ACCT_ID_FIELD,
)
DONE_SCREEN = ExpectedScreen(
    UpdateScreenState.CHANGES_DONE, NO_MESSAGE, UPDATE_SUCCESS_INFO, ACCT_ID_FIELD
)
EMPTY_KEY_SCREEN = ExpectedScreen(
    UpdateScreenState.DETAILS_NOT_FETCHED, NO_MESSAGE, PROMPT_FOR_KEYS_INFO, ACCT_ID_FIELD
)
NO_CHANGE_REFUSAL = ExpectedRefusal(
    HTTP_EDIT_FAILURE,
    NO_CHANGES_MESSAGE,
    DETAILS_SHOWN_INFO,
    (),
    NAME_FIELD,
    UpdateScreenState.SHOW_DETAILS,
)
NOT_FOUND_REFUSAL = ExpectedRefusal(
    HTTP_NOT_FOUND,
    CARD_NOT_FOUND_MESSAGE,
    PROMPT_FOR_KEYS_INFO,
    BOTH_KEY_FIELDS,
    ACCT_ID_FIELD,
    UpdateScreenState.DETAILS_NOT_FETCHED,
)
READ_FILE_ERROR_REFUSAL = ExpectedRefusal(
    HTTP_FILE_ERROR,
    READ_FILE_ERROR_MESSAGE,
    PROMPT_FOR_KEYS_INFO,
    (ACCT_ID_FIELD,),
    ACCT_ID_FIELD,
    UpdateScreenState.DETAILS_NOT_FETCHED,
)
RESTART_REFUSAL = ExpectedRefusal(
    HTTP_EDIT_FAILURE,
    NO_INPUT_RECEIVED_MESSAGE,
    PROMPT_FOR_KEYS_INFO,
    BOTH_KEY_FIELDS,
    ACCT_ID_FIELD,
    UpdateScreenState.DETAILS_NOT_FETCHED,
)
LOCK_REFUSAL = ExpectedRefusal(
    HTTP_RECORD_LOCKED,
    LOCK_FAILED_MESSAGE,
    UPDATE_FAILURE_INFO,
    (),
    ACCT_ID_FIELD,
    UpdateScreenState.CHANGES_FAILED,
)
CHANGED_REFUSAL = ExpectedRefusal(
    HTTP_RECORD_CHANGED,
    RECORD_CHANGED_MESSAGE,
    DETAILS_SHOWN_INFO,
    (),
    NAME_FIELD,
    UpdateScreenState.SHOW_DETAILS,
)
UPDATE_FAILED_REFUSAL = ExpectedRefusal(
    HTTP_FILE_ERROR,
    UPDATE_FAILED_MESSAGE,
    UPDATE_FAILURE_INFO,
    (),
    ACCT_ID_FIELD,
    UpdateScreenState.CHANGES_FAILED,
)


def ReadCardRow(syncEngine: Engine, pan: str) -> CardRow | None:
    """Return the cards.cards row of ``pan`` read with plain SQL, or None when it is absent."""
    with syncEngine.connect() as connection:
        row = connection.execute(CARD_ROW_QUERY, {"cardNum": pan}).first()
    return CardRow(*row) if row is not None else None


def RenameCard(syncEngine: Engine, pan: str, name: str) -> None:
    """Commit another task's change of the card's embossed name, outside the service."""
    with syncEngine.begin() as connection:
        connection.execute(RENAME_CARD_SQL, {"name": name, "cardNum": pan})


@pytest_asyncio.fixture(name="cardScenario")
async def CardScenarioFixture(makeOnlineContext, syncEngine, restoreSeedState) -> CardScenario:
    """Return a regular user's context, the engine and the seed card's row; restore the seed."""
    context = await makeOnlineContext(REGULAR_USER_TYPE)
    return CardScenario(context, syncEngine, ReadCardRow(syncEngine, SEED_CARD_NUM))


def SeedScreenFields(cardRow: CardRow) -> CardScreenFields:
    """Return the COCRDUP fields 3200 shows from CCUP-OLD-* of ``cardRow`` (lines 1107-1112).

    The name is upper-cased (INSPECT CONVERTING, lines 1354-1356), the card number masked,
    and the dark EXPDAY is never shown.
    """
    return CardScreenFields(
        acct_id=SEED_ACCT_ID,
        card_num=MASKED_SEED_CARD_NUM,
        embossed_name=cardRow.name.upper(),
        active_status=cardRow.status,
        expiry_month=f"{cardRow.expiry.month:02d}",
        expiry_year=f"{cardRow.expiry.year:04d}",
    )


async def FetchCardScreen(context: Any, pan: str, acctId: str) -> CardScreen:
    """Open the card for update with typed keys, as ENTER on the empty key screen does."""
    return await cocrdupc.FetchCardForEdit(context, pan, acctId)


def WithCardChanges(
    screen: CardScreen, changes: dict[str, str], action: UpdateAction = UpdateAction.ENTER
) -> CardUpdateRequest:
    """Return the next key on ``screen``: its token, and its fields with ``changes`` keyed."""
    keyedFields = CardScreenFields(**{**screen.fields.model_dump(), **changes})
    return CardUpdateRequest(action=action, screen_token=screen.screen_token, fields=keyedFields)


async def SendKey(scenario: CardScenario, request: CardUpdateRequest) -> CardScreen:
    """Send one ENTER, F5 or F12 on the seed card's ``{cardNum}`` path."""
    return await cocrdupc.ProcessCardUpdate(scenario.context, SEED_CARD_NUM, request)


async def SendRefusedKey(scenario: CardScenario, request: CardUpdateRequest) -> LegacyMessageError:
    """Send one key the service refuses and return the LegacyMessageError it raises."""
    with pytest.raises(LegacyMessageError) as raised:
        await SendKey(scenario, request)
    return raised.value


async def FetchRefusal(context: Any, cardKey: str, acctText: str) -> LegacyMessageError:
    """Open a card with keys the service refuses and return the LegacyMessageError it raises."""
    with pytest.raises(LegacyMessageError) as raised:
        await FetchCardScreen(context, cardKey, acctText)
    return raised.value


async def ValidatedScreen(scenario: CardScenario, changes: dict[str, str]) -> CardScreen:
    """Fetch the seed card and ENTER ``changes``: the CHANGES_OK_NOT_CONFIRMED screen."""
    fetched = await FetchCardScreen(scenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    return await SendKey(scenario, WithCardChanges(fetched, changes))


async def SavedScreen(scenario: CardScenario, changes: dict[str, str]) -> CardScreen:
    """Validate ``changes`` on the seed card, then press F5: the CHANGES_DONE screen."""
    validated = await ValidatedScreen(scenario, changes)
    return await SendKey(scenario, WithCardChanges(validated, {}, UpdateAction.SAVE))


def AssertScreen(screen: CardScreen, expected: ExpectedScreen) -> None:
    """Assert a CardScreen's state, both message lines and cursor, with no failing field."""
    assert screen.state == expected.state
    assert (screen.message, screen.info_message) == (expected.message, expected.info)
    assert (screen.field_errors, screen.cursor_field) == ([], expected.cursorField)


def AssertRefusal(refusal: LegacyMessageError, expected: ExpectedRefusal) -> dict[str, Any]:
    """Assert a refusal's status and body against ``expected`` and return the body."""
    body = refusal.Body()
    assert refusal.httpStatus == expected.httpStatus
    assert set(body) == CCUP_ERROR_BODY_KEYS
    assert (body["message"], body["info_message"]) == (expected.message, expected.info)
    assert tuple(body["field_errors"]) == expected.fieldErrors
    assert body["cursor_field"] == expected.cursorField
    assert body["state"] == expected.state
    return body


def AssertSeedRowUnchanged(scenario: CardScenario) -> None:
    """Assert that no rewrite reached the seed card: its row equals the one read at setup."""
    assert ReadCardRow(scenario.syncEngine, SEED_CARD_NUM) == scenario.seedRow


def ForgedScreenToken(screenToken: str, settings: Any) -> str:
    """Return the token's payload sealed under a SECRET_KEY the server does not hold."""
    payload = DeriveFernet(settings.SECRET_KEY, SCREEN_STATE_HKDF_INFO).decrypt(screenToken)
    otherKey = secrets.token_urlsafe(OTHER_SECRET_KEY_BYTES)
    return DeriveFernet(otherKey, SCREEN_STATE_HKDF_INFO).encrypt(payload).decode("ascii")


def ExpiredScreenToken(screenToken: str, settings: Any) -> str:
    """Return the token's payload sealed again by the server, issued past its lifetime."""
    screenCipher = DeriveFernet(settings.SECRET_KEY, SCREEN_STATE_HKDF_INFO)
    lifetimeSeconds = settings.ACCESS_TOKEN_EXPIRE_MINUTES * SECONDS_PER_MINUTE
    issuedAt = int(time.time()) - lifetimeSeconds - EXPIRY_MARGIN_SECONDS
    payload = screenCipher.decrypt(screenToken)
    return screenCipher.encrypt_at_time(payload, issuedAt).decode("ascii")


async def RaiseRecordLock(repository: CardRepository, unit: AsyncConnection, cardNum: str) -> None:
    """Stand in for READ UPDATE answering RECORDBUSY: another task holds the card record."""
    raise RecordLockError(CARD_FILE_NAME)


async def RaiseFileIo(
    repository: CardRepository, unit: AsyncConnection, values: Mapping[str, Any]
) -> None:
    """Stand in for a REWRITE of CARDDAT answering IOERR."""
    raise FileIoError(CARD_FILE_NAME)


async def RaiseReadFileIo(repository: CardRepository, cardNum: str) -> None:
    """Stand in for the 9100 READ of CARDDAT answering IOERR."""
    raise FileIoError(CARD_FILE_NAME)


async def FetchForEditReadRefusal(
    scenario: CardScenario, monkeypatch: pytest.MonkeyPatch
) -> LegacyMessageError:
    """Open the seed card with typed keys while its READ answers IOERR; return the refusal."""
    monkeypatch.setattr(CardRepository, "GetCard", RaiseReadFileIo)
    return await FetchRefusal(scenario.context, SEED_CARD_NUM, SEED_ACCT_ID)


async def RereadReadRefusal(
    scenario: CardScenario, monkeypatch: pytest.MonkeyPatch
) -> LegacyMessageError:
    """Fetch the seed card, then press F12 while its re-read answers IOERR; return the refusal."""
    fetched = await FetchCardScreen(scenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    monkeypatch.setattr(CardRepository, "GetCard", RaiseReadFileIo)
    return await SendRefusedKey(scenario, WithCardChanges(fetched, {}, UpdateAction.CANCEL))


# --- 1230-1260 detail edits (lines 806-944) ----------------------------------------------------
class FieldEditCase(NamedTuple):
    """One detail value 1230-1260 refuse, its literal, its field and the value then shown.

    A blank failing field shows '*' (3300); EXPMON is JUSTIFY=(RIGHT), so ``0`` shows `` 0``.
    """

    changes: dict[str, str]
    message: str
    failingField: str
    shownValue: str


FIELD_EDIT_CASES = [
    FieldEditCase({NAME_FIELD: ""}, CARD_NAME_NOT_PROVIDED_MESSAGE, NAME_FIELD, BLANK_KEY_MARKER),
    FieldEditCase({NAME_FIELD: "JOHN 3RD"}, CARD_NAME_ALPHA_MESSAGE, NAME_FIELD, "JOHN 3RD"),
    FieldEditCase({STATUS_FIELD: "X"}, CARD_STATUS_MESSAGE, STATUS_FIELD, "X"),
    FieldEditCase({MONTH_FIELD: "13"}, EXPIRY_MONTH_MESSAGE, MONTH_FIELD, "13"),
    FieldEditCase({MONTH_FIELD: "0"}, EXPIRY_MONTH_MESSAGE, MONTH_FIELD, " 0"),
    FieldEditCase({YEAR_FIELD: "1949"}, EXPIRY_YEAR_MESSAGE, YEAR_FIELD, "1949"),
    FieldEditCase({YEAR_FIELD: "2100"}, EXPIRY_YEAR_MESSAGE, YEAR_FIELD, "2100"),
]
FIELD_EDIT_IDS = [
    "blank-name",
    "name-with-digit",
    "status-x",
    "month-13",
    "month-0",
    "year-1949",
    "year-2100",
]


@pytest.mark.parametrize("fieldCase", FIELD_EDIT_CASES, ids=FIELD_EDIT_IDS)
async def test_detail_edit_refuses_field(cardScenario, fieldCase):
    """ENTER with one refused detail value: 400, its literal, CHANGES_NOT_OK, no write."""
    fetched = await FetchCardScreen(cardScenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    refusal = await SendRefusedKey(cardScenario, WithCardChanges(fetched, fieldCase.changes))
    expected = ExpectedRefusal(
        HTTP_EDIT_FAILURE,
        fieldCase.message,
        PROMPT_FOR_CHANGES_INFO,
        (fieldCase.failingField,),
        fieldCase.failingField,
        UpdateScreenState.CHANGES_NOT_OK,
    )
    body = AssertRefusal(refusal, expected)
    assert body["fields"][fieldCase.failingField] == fieldCase.shownValue
    AssertSeedRowUnchanged(cardScenario)


@pytest.mark.parametrize("expiryYear", VALID_BOUNDARY_YEARS)
async def test_boundary_expiry_year_passes_validation(cardScenario, expiryYear):
    """88 VALID-YEAR holds 1950 and 2099: the change is validated and nothing is written."""
    validated = await ValidatedScreen(cardScenario, {YEAR_FIELD: expiryYear})
    AssertScreen(validated, CONFIRM_SCREEN)
    assert validated.fields.expiry_year == expiryYear
    AssertSeedRowUnchanged(cardScenario)


async def test_two_refused_fields_keep_first_message_and_both_fields(cardScenario):
    """All-field mode: status X and month 13 give the status literal and both fields."""
    fetched = await FetchCardScreen(cardScenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    changes = {STATUS_FIELD: "X", MONTH_FIELD: "13"}
    refusal = await SendRefusedKey(cardScenario, WithCardChanges(fetched, changes))
    expected = ExpectedRefusal(
        HTTP_EDIT_FAILURE,
        CARD_STATUS_MESSAGE,
        PROMPT_FOR_CHANGES_INFO,
        (STATUS_FIELD, MONTH_FIELD),
        STATUS_FIELD,
        UpdateScreenState.CHANGES_NOT_OK,
    )
    AssertRefusal(refusal, expected)
    AssertSeedRowUnchanged(cardScenario)


# --- 1210-EDIT-ACCOUNT and 1220-EDIT-CARD on the typed keys (lines 721-800) --------------------
class KeyEditCase(NamedTuple):
    """Typed keys the key edits refuse, the literal kept and every failing key in order."""

    cardKey: str
    acctText: str
    message: str
    fieldErrors: tuple[str, ...]


KEY_EDIT_CASES = [
    KeyEditCase("", "", NO_INPUT_RECEIVED_MESSAGE, BOTH_KEY_FIELDS),
    KeyEditCase(BLANK_KEY_MARKER, BLANK_KEY_MARKER, NO_INPUT_RECEIVED_MESSAGE, BOTH_KEY_FIELDS),
    KeyEditCase(SEED_CARD_NUM, BLANK_KEY_MARKER, ACCOUNT_NOT_PROVIDED_MESSAGE, (ACCT_ID_FIELD,)),
    KeyEditCase(BLANK_KEY_MARKER, SEED_ACCT_ID, CARD_NOT_PROVIDED_MESSAGE, (CARD_NUM_FIELD,)),
    KeyEditCase(SEED_CARD_NUM, NON_NUMERIC_ACCT_TEXT, ACCOUNT_FILTER_MESSAGE, (ACCT_ID_FIELD,)),
    KeyEditCase(NON_NUMERIC_CARD_TEXT, SEED_ACCT_ID, CARD_FILTER_MESSAGE, (CARD_NUM_FIELD,)),
]
KEY_EDIT_IDS = [
    "both-blank",
    "both-asterisk",
    "account-asterisk",
    "card-asterisk",
    "account-non-numeric",
    "card-non-numeric",
]


@pytest.mark.parametrize("keyCase", KEY_EDIT_CASES, ids=KEY_EDIT_IDS)
async def test_fetch_refuses_invalid_keys(cardScenario, keyCase):
    """FetchCardForEdit edits both typed keys as COCRDSLC does and reads nothing on failure."""
    refusal = await FetchRefusal(cardScenario.context, keyCase.cardKey, keyCase.acctText)
    expected = ExpectedRefusal(
        HTTP_EDIT_FAILURE,
        keyCase.message,
        PROMPT_FOR_KEYS_INFO,
        keyCase.fieldErrors,
        keyCase.fieldErrors[0],
        UpdateScreenState.DETAILS_NOT_FETCHED,
    )
    body = AssertRefusal(refusal, expected)
    assert body["fields"][ACCT_ID_FIELD] == keyCase.acctText


async def test_fetch_of_missing_card_is_not_found(cardScenario):
    """Valid keys naming no CARDDAT record: 404 DID-NOT-FIND-ACCTCARD-COMBO on the key screen."""
    assert ReadCardRow(cardScenario.syncEngine, MISSING_CARD_NUM) is None
    refusal = await FetchRefusal(cardScenario.context, MISSING_CARD_NUM, SEED_ACCT_ID)
    body = AssertRefusal(refusal, NOT_FOUND_REFUSAL)
    assert body["fields"][ACCT_ID_FIELD] == SEED_ACCT_ID


@pytest.mark.parametrize(
    "readEntry",
    [FetchForEditReadRefusal, RereadReadRefusal],
    ids=["fetch-for-edit", "f12-reread"],
)
async def test_card_read_error_shows_file_error_on_key_screen(cardScenario, monkeypatch, readEntry):
    """A CARDDAT READ ending WHEN OTHER: 503 WS-FILE-ERROR-MESSAGE, account flagged, no write."""
    refusal = await readEntry(cardScenario, monkeypatch)
    body = AssertRefusal(refusal, READ_FILE_ERROR_REFUSAL)
    shownKeys = (body["fields"][ACCT_ID_FIELD], body["fields"][CARD_NUM_FIELD])
    assert shownKeys == (SEED_ACCT_ID, MASKED_SEED_CARD_NUM)
    AssertSeedRowUnchanged(cardScenario)


# --- CCUP-CHANGE-ACTION state machine (lines 517-1022) -----------------------------------------
async def test_enter_before_fetch_runs_only_key_edits(cardScenario):
    """Before fetch only the keys are edited: refused detail values are ignored, card shown."""
    keyedFields = CardScreenFields(
        acct_id=SEED_ACCT_ID, card_num=SEED_CARD_NUM, **INVALID_DETAIL_VALUES
    )
    screen = await SendKey(cardScenario, CardUpdateRequest(fields=keyedFields))
    AssertScreen(screen, SHOWN_SCREEN)
    assert screen.fields == SeedScreenFields(cardScenario.seedRow)
    assert DecodeCardRef(screen.card_ref, cardScenario.context.settings) == SEED_CARD_NUM
    AssertSeedRowUnchanged(cardScenario)


async def test_unchanged_details_are_no_change_whatever_the_case(cardScenario):
    """The fetched name keyed in lower case is NO-CHANGES-DETECTED: state kept, no write."""
    fetched = await FetchCardScreen(cardScenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    changes = {NAME_FIELD: cardScenario.seedRow.name.lower()}
    refusal = await SendRefusedKey(cardScenario, WithCardChanges(fetched, changes))
    body = AssertRefusal(refusal, NO_CHANGE_REFUSAL)
    assert body["fields"] == SeedScreenFields(cardScenario.seedRow).model_dump(mode="json")
    AssertSeedRowUnchanged(cardScenario)


async def test_valid_change_waits_for_confirmation(cardScenario):
    """A valid change gives CHANGES_OK_NOT_CONFIRMED with the F5 prompt and writes nothing."""
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    AssertScreen(validated, CONFIRM_SCREEN)
    assert validated.fields.active_status == NEW_STATUS
    AssertSeedRowUnchanged(cardScenario)


async def test_enter_while_confirming_runs_no_edit(cardScenario):
    """ENTER on the validated screen runs no edit: refused values are ignored, state kept."""
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    screen = await SendKey(cardScenario, WithCardChanges(validated, INVALID_DETAIL_VALUES))
    AssertScreen(screen, CONFIRM_SCREEN)
    assert screen.fields == validated.fields
    AssertSeedRowUnchanged(cardScenario)


async def test_save_before_validation_only_validates(cardScenario):
    """F5 with details shown is processed as ENTER: the change is validated, not written."""
    fetched = await FetchCardScreen(cardScenario.context, SEED_CARD_NUM, SEED_ACCT_ID)
    changes = {STATUS_FIELD: NEW_STATUS}
    screen = await SendKey(cardScenario, WithCardChanges(fetched, changes, UpdateAction.SAVE))
    AssertScreen(screen, CONFIRM_SCREEN)
    assert screen.fields.active_status == NEW_STATUS
    AssertSeedRowUnchanged(cardScenario)


async def test_save_writes_the_validated_values(cardScenario):
    """F5 after validation rewrites the token's values, whatever fields the request sends."""
    validated = await ValidatedScreen(
        cardScenario, {NAME_FIELD: NEW_NAME, STATUS_FIELD: NEW_STATUS}
    )
    saveRequest = WithCardChanges(validated, OTHER_DETAIL_VALUES, UpdateAction.SAVE)
    saved = await SendKey(cardScenario, saveRequest)
    AssertScreen(saved, DONE_SCREEN)
    expectedRow = cardScenario.seedRow._replace(name=NEW_NAME, status=NEW_STATUS)
    assert ReadCardRow(cardScenario.syncEngine, SEED_CARD_NUM) == expectedRow


async def test_cancel_rereads_the_card(cardScenario):
    """F12 on the validated screen discards the change and shows the card as stored."""
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    screen = await SendKey(cardScenario, WithCardChanges(validated, {}, UpdateAction.CANCEL))
    AssertScreen(screen, SHOWN_SCREEN)
    assert screen.fields == SeedScreenFields(cardScenario.seedRow)
    AssertSeedRowUnchanged(cardScenario)


@pytest.mark.parametrize("action", list(UpdateAction))
async def test_key_after_changes_done_shows_empty_key_screen(cardScenario, action):
    """Every key after CHANGES_DONE discards the input and sends the empty key screen."""
    saved = await SavedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    screen = await SendKey(cardScenario, WithCardChanges(saved, INVALID_DETAIL_VALUES, action))
    AssertScreen(screen, EMPTY_KEY_SCREEN)
    assert screen.fields == CardScreenFields()
    assert screen.card_ref == ""


@pytest.mark.parametrize(
    "tokenBuilder", [ForgedScreenToken, ExpiredScreenToken], ids=["forged", "expired"]
)
async def test_invalid_screen_token_restarts_flow(cardScenario, tokenBuilder):
    """A forged or expired token of a validated change is DETAILS_NOT_FETCHED: F5 writes nothing."""
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    badToken = tokenBuilder(validated.screen_token, cardScenario.context.settings)
    request = CardUpdateRequest(action=UpdateAction.SAVE, screen_token=badToken)
    refusal = await SendRefusedKey(cardScenario, request)
    AssertRefusal(refusal, RESTART_REFUSAL)
    AssertSeedRowUnchanged(cardScenario)


# --- 9200-WRITE-PROCESSING outcomes (lines 1420-1511) ------------------------------------------
async def test_save_refused_when_record_locked(cardScenario, monkeypatch):
    """READ UPDATE answering RECORDBUSY is COULD-NOT-LOCK-FOR-UPDATE: 423, nothing written."""
    monkeypatch.setattr(CardRepository, "GetCardForUpdate", RaiseRecordLock)
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    saveRequest = WithCardChanges(validated, {}, UpdateAction.SAVE)
    refusal = await SendRefusedKey(cardScenario, saveRequest)
    body = AssertRefusal(refusal, LOCK_REFUSAL)
    assert body["fields"][STATUS_FIELD] == NEW_STATUS
    AssertSeedRowUnchanged(cardScenario)


async def test_save_refused_when_record_changed_since_fetch(cardScenario):
    """A record changed after fetch is DATA-WAS-CHANGED-BEFORE-UPDATE: 409 with fresh data."""
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    RenameCard(cardScenario.syncEngine, SEED_CARD_NUM, CONCURRENT_NAME)
    saveRequest = WithCardChanges(validated, {}, UpdateAction.SAVE)
    refusal = await SendRefusedKey(cardScenario, saveRequest)
    body = AssertRefusal(refusal, CHANGED_REFUSAL)
    freshRow = cardScenario.seedRow._replace(name=CONCURRENT_NAME)
    assert body["fields"] == SeedScreenFields(freshRow).model_dump(mode="json")
    assert ReadCardRow(cardScenario.syncEngine, SEED_CARD_NUM) == freshRow


async def test_save_refused_when_rewrite_fails(cardScenario, monkeypatch):
    """A REWRITE answering IOERR is LOCKED-BUT-UPDATE-FAILED: 503, nothing written."""
    monkeypatch.setattr(CardRepository, "UpdateCard", RaiseFileIo)
    validated = await ValidatedScreen(cardScenario, {STATUS_FIELD: NEW_STATUS})
    saveRequest = WithCardChanges(validated, {}, UpdateAction.SAVE)
    refusal = await SendRefusedKey(cardScenario, saveRequest)
    body = AssertRefusal(refusal, UPDATE_FAILED_REFUSAL)
    assert body["fields"][STATUS_FIELD] == NEW_STATUS
    AssertSeedRowUnchanged(cardScenario)


async def test_saved_expiry_keeps_day_and_cvv(cardScenario):
    """A new expiry year and month are written with the before-image day; the CVV is kept.

    The name keyed back is the upper-cased one the screen showed, and it is written as sent.
    """
    saved = await SavedScreen(cardScenario, {MONTH_FIELD: NEW_MONTH, YEAR_FIELD: NEW_YEAR})
    AssertScreen(saved, DONE_SCREEN)
    seedRow = cardScenario.seedRow
    expectedExpiry = date(int(NEW_YEAR), int(NEW_MONTH), seedRow.expiry.day)
    savedRow = ReadCardRow(cardScenario.syncEngine, SEED_CARD_NUM)
    assert savedRow == seedRow._replace(name=seedRow.name.upper(), expiry=expectedExpiry)
    assert (savedRow.cvv, savedRow.acctId) == (seedRow.cvv, SEED_ACCT_ID)
